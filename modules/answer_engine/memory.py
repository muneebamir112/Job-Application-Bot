"""Short-term memory: the 'form so far' notes and the answered turns, append-only. Trimming only on a real overflow, from the middle."""
import json
import re

from .lexicons import DEMOGRAPHIC_RX, LEDGER_TOPICS


class Memory:
    def __init__(self, system: str, num_ctx: int):
        self.messages = [{"role": "system", "content": system}]
        self.num_ctx = num_ctx
        self.ledger: dict[str, str] = {}      # topic -> value (authorization / sponsorship / salary)
        self.pending: list[str] = []          # form-so-far lines not yet sent to the model
        self.cpt = 3.6                        # rolling characters per token
        self.events: list[dict] = []          # trimming events, for the audit file
        self.turn = 0

    # ------------------------------------------------ form so far
    def remember(self, label: str, value) -> None:
        label = (label or "").strip()
        if not label or value is None:
            return
        text = ("Yes" if value else "No") if isinstance(value, bool) else str(value).strip()
        if not text:
            return
        for name, rx in LEDGER_TOPICS:
            if re.search(rx, label.lower()):
                self.ledger[name] = text
        if DEMOGRAPHIC_RX.search(label):
            return
        self.pending.append(f"- {label[:120]}: {text[:120]}")

    def flush_pending(self) -> None:
        """Turn the notes into one small protected user/assistant pair at the END (the cached prefix stays untouched)."""
        if not self.pending:
            return
        self.messages.append({"role": "user", "_keep": True,
                              "content": "FORM SO FAR (answers already submitted by the form logic):\n" + "\n".join(self.pending)})
        self.messages.append({"role": "assistant", "_keep": True, "content": json.dumps({"answer": "Noted.", "used_bullets": []})})
        self.pending = []

    # ------------------------------------------------ turns
    def add_turn(self, user: str, assistant_json: str) -> None:
        self.messages.append({"role": "user", "content": user})
        self.messages.append({"role": "assistant", "content": assistant_json})

    def view(self, extra_user: str) -> list[dict]:
        return [{"role": m["role"], "content": m["content"]} for m in self.messages] + [{"role": "user", "content": extra_user}]

    def observe(self, sent_chars: int, prompt_tokens: int) -> None:
        if prompt_tokens:
            self.cpt = 0.7 * self.cpt + 0.3 * (sent_chars / prompt_tokens)

    # ------------------------------------------------ trimming
    def fit(self, extra_chars: int, reserve: int) -> None:
        """Only if the real prompt would overflow: drop whole turns from the middle.
        Protected: system message, form-so-far pairs, the first 2 and the last 3 turns."""
        budget = self.num_ctx - reserve - 300
        est = lambda: (sum(len(m["content"]) for m in self.messages) + extra_chars) / self.cpt
        before = est()
        if before <= budget:
            return
        dropped = 0
        while est() > budget:
            turns = [(i, i + 1) for i in range(1, len(self.messages) - 1, 2) if not self.messages[i].get("_keep")]
            mid = turns[2:max(2, len(turns) - 3)]
            if mid:
                i, j = mid[len(mid) // 2]
            elif len(turns) > 3:
                i, j = turns[0]
            else:
                break
            del self.messages[i:j + 1]
            dropped += 2
        self.events.append({"turn": self.turn, "dropped_msgs": dropped, "est_before": round(before), "est_after": round(est()), "budget": budget})
