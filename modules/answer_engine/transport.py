"""Async Ollama /api/chat transport with metrics, retries and a circuit breaker. Uses the model from config; never names one."""
import asyncio
import json
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)


class TransportError(Exception):
    pass


class OllamaTransport:
    def __init__(self, host: str, model: str, num_ctx: int, temperature: float, keep_alive: str, think: bool, timeout: int = 300):
        self.host, self.model = host.rstrip("/"), model
        self.num_ctx, self.temperature, self.keep_alive, self.think, self.timeout = num_ctx, temperature, keep_alive, think, timeout
        self.failures = 0                # consecutive failures
        self.send_think = True           # flips to False if the model rejects the `think` flag

    @property
    def degraded(self) -> bool:
        return self.failures >= 3

    async def chat(self, messages: list[dict], num_predict: int, fmt: dict | None = None, temperature: float | None = None) -> dict:
        """Returns {"content", "secs", "ptok", "cached", "gen_tokens", "think_chars", "done", "sent_chars"}; raises TransportError."""
        if self.degraded:
            raise TransportError("Ollama marked unavailable for this application (3 consecutive failures)")
        last_err = None
        for attempt in range(3):
            body = {"model": self.model, "stream": False, "keep_alive": self.keep_alive,
                    "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
                    "options": {"temperature": self.temperature if temperature is None else temperature,
                                "num_ctx": self.num_ctx, "num_predict": num_predict}}
            if self.send_think:
                body["think"] = self.think
            if fmt is not None:
                body["format"] = fmt
            t0 = time.time()
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(f"{self.host}/api/chat", json=body, timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                        if r.status == 400 and self.send_think:
                            txt = await r.text()
                            if "think" in txt.lower():       # model does not support the flag: retry once without it
                                self.send_think = False
                                continue
                            raise TransportError(f"HTTP 400: {txt[:200]}")
                        if r.status != 200:
                            raise TransportError(f"HTTP {r.status}: {(await r.text())[:200]}")
                        d = await r.json()
                m = d.get("message") or {}
                self.failures = 0
                return {"content": (m.get("content") or "").strip(), "secs": round(time.time() - t0, 1),
                        "ptok": d.get("prompt_eval_count") or 0, "cached": d.get("prompt_eval_cached_count") or 0,
                        "gen_tokens": d.get("eval_count") or 0, "think_chars": len(m.get("thinking") or ""),
                        "done": d.get("done_reason"), "sent_chars": sum(len(x["content"]) for x in messages)}
            except TransportError as e:
                last_err = e
                break
            except Exception as e:                           # network / timeout
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(2)
        self.failures += 1
        logger.error(f"[answer_engine] Ollama call failed ({self.failures} in a row): {last_err}")
        raise TransportError(str(last_err))


def parse_json(content: str, key: str = "answer"):
    """Parse a reply; returns the dict only if it has the expected key and a used_bullets list (or the number schema)."""
    try:
        o = json.loads(content)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    if key == "number":
        return o if isinstance(o.get("number"), (int, float)) else None
    if isinstance(o.get(key), str) and isinstance(o.get("used_bullets"), list):
        return o
    return None
