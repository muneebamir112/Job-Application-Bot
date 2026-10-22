"""AnswerEngine: one per job application. Owns the long-term block (fact sheet + job description), the short-term memory and the ladder."""
import difflib
import json
import logging
import os
import re

from . import ladder, prompts
from .facts import FactSheet, build_fact_sheet, years_for
from .guard import Guard
from .lexicons import TECH, stem
from .memory import Memory
from .result import AnswerResult
from .router import Router, question_terms
from .transport import OllamaTransport, TransportError, parse_json

logger = logging.getLogger(__name__)

_PLACEHOLDERS = {"", "select", "select...", "choose", "choose...", "please select", "please select...", "-- select --", "--", "select an option", "select one"}
_DECLINE = ("decline", "prefer not", "not applicable", "n/a", "none of the above", "i don't wish", "do not wish")
_EXPERIENCE_Q = re.compile(r"experience|years|proficien|familiar|worked with|do you have|are you able|certif|licen", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#% ]", " ", (s or "").lower())).strip()


class AnswerEngine:
    def __init__(self, profile, sheet: FactSheet, company: str, title: str, jd: str, settings: dict):
        self.profile, self.sheet, self.company, self.title, self.settings = profile, sheet, company, title, settings
        self.jd = jd or ""
        self._fit_jd(settings["num_ctx"])
        system = prompts.build_system(sheet.p1, sheet.render(), company, title, self.jd)
        self.system = system
        self.memory = Memory(system, settings["num_ctx"])
        self.transport = OllamaTransport(settings["host"], settings["model"], settings["num_ctx"], settings["temperature"],
                                         settings["keep_alive"], settings["think"])
        self.router = Router(sheet.text(), company)
        self.guard = Guard(sheet, company, self.jd)
        self.results: list[AnswerResult] = []

    # ------------------------------------------------ construction
    @classmethod
    async def create(cls, profile: dict, resume_text: str, company: str, title: str, jd: str, profile_name: str, settings: dict) -> "AnswerEngine":
        sheet = build_fact_sheet(profile, resume_text, company, profile_name, settings["facts_source"], settings["resume_data_dir"])
        eng = cls(profile, sheet, company, title, jd, settings)
        n = sum(1 for f in sheet.facts if f["kind"] == "W")
        logger.info(f"[answer_engine] facts source={sheet.source} total={len(sheet.facts)} work={n} system_chars={len(eng.system)}")
        if n < 3:
            logger.warning("[answer_engine] fewer than 3 work facts found; answers will be mostly yellow fallbacks")
        return eng

    def _fit_jd(self, num_ctx: int) -> None:
        """The long-term block must stay below ~60% of the window; cut the job description at a sentence boundary if needed."""
        budget_chars = int(num_ctx * 0.6 * 3.6)
        fixed = len(prompts.INSTRUCTIONS) + len(self.sheet.p1) + len(self.sheet.render()) + 200
        room = max(1500, budget_chars - fixed)
        if len(self.jd) > room:
            cut = self.jd[:room]
            end = max(cut.rfind(". "), cut.rfind("\n"))
            self.jd = cut[:end + 1] if end > room * 0.5 else cut
            logger.info(f"[answer_engine] job description cut to {len(self.jd)} chars to fit the window")

    # ------------------------------------------------ memory hook
    def remember(self, label: str, value) -> None:
        try:
            self.memory.remember(label, value)
        except Exception:
            pass

    # ------------------------------------------------ open-ended text
    async def answer_open(self, question: str) -> AnswerResult:
        try:
            res = await ladder.answer_open(self, question)
        except Exception as e:                      # never let the engine break the form filler
            logger.error(f"[answer_engine] unexpected error for '{question}': {e}")
            res = AnswerResult(text=ladder.restate_line(self, question, "FACTUAL", []) or ladder.summary_sentence(self),
                               color="YELLOW", via="restate", route="FACTUAL", question=question)
        if not res.text.strip():
            res.text, res.via, res.color = ladder.summary_sentence(self), "summary", "YELLOW"
        self.results.append(res)
        return res

    # ------------------------------------------------ choices (dropdown / radio / checkbox / yes-no)
    def _match_option(self, choice: str, options: list[str]) -> str | None:
        c = _norm(choice)
        for o in options:
            if _norm(o) == c and c:
                return o.strip()
        best, score = None, 0.0
        for o in options:
            sc = difflib.SequenceMatcher(None, c, _norm(o)).ratio()
            if sc > score:
                best, score = o, sc
        return best.strip() if best and score >= 0.85 else None

    def _fallback_choice(self, options: list[str]) -> str:
        real = [o for o in options if _norm(o) not in _PLACEHOLDERS]
        for o in real:
            if any(k in o.lower() for k in _DECLINE):
                return o.strip()
        return (real or options or [""])[0].strip()

    async def answer_choice(self, question: str, options: list[str]) -> AnswerResult:
        res = AnswerResult(text="", route="CHOICE", question=question)
        real = [o for o in options if _norm(o) not in _PLACEHOLDERS]
        mem = self.memory
        try:
            _, missing, _ = self.router.classify(question, mem.ledger)
            res.missing_words = missing
            no_opt = next((o for o in real if re.match(r"^(no|none|0|never)\b", _norm(o))), None)
            if self.router.classify(question, {})[0] == "DECLINE":                # demographic: prefer-not-to-say option, never a guess
                res.text, res.via = self._fallback_choice(options), "rule"
            elif missing and no_opt and _EXPERIENCE_Q.search(question):          # the resume says nothing about it: honest "No"
                res.text, res.via = no_opt.strip(), "rule"
            elif self.transport.degraded or not real:
                res.text, res.via = self._fallback_choice(options), "choice-fallback"
            else:
                opts = "\n".join(f"- {o}" for o in real)
                feedback = ""
                for attempt in (1, 2):
                    blocked = f"The fact sheet says nothing about: {', '.join(missing)}. Do not claim it. " if missing else ""
                    user = f"QUESTION: {question}\nTASK: {prompts.CHOICE_TASK.format(blocked=blocked, options=opts)}{feedback}"
                    mem.flush_pending()
                    mem.fit(len(user), 120)
                    try:
                        r = await self.transport.chat(mem.view(user), 120, fmt=prompts.CHOICE_SCHEMA, temperature=0.0)
                    except TransportError as e:
                        res.attempts.append({"attempt": attempt, "answer": "", "used": [], "reasons": [f"model unavailable: {e}"]})
                        break
                    mem.observe(r["sent_chars"], r["ptok"])
                    res.calls.append({k: r[k] for k in ("secs", "ptok", "cached", "gen_tokens", "think_chars", "done")})
                    obj = parse_json(r["content"], key="choice")
                    reasons, picked = [], None
                    if obj is None:
                        reasons.append("reply was not valid JSON")
                    else:
                        picked = self._match_option(obj["choice"], real)
                        used = [i for i in obj.get("used_bullets", []) if i in self.sheet.index]
                        res.cited_ids = used
                        if not picked:
                            reasons.append("the choice is not one of the options (copy one exactly)")
                        elif re.match(r"^yes\b", _norm(picked)) and _EXPERIENCE_Q.search(question) and not used:
                            reasons.append("answering Yes about experience needs a cited fact from the fact sheet")
                    res.attempts.append({"attempt": attempt, "answer": (obj or {}).get("choice", r["content"][:100]), "used": res.cited_ids, "reasons": reasons})
                    if not reasons:
                        res.text, res.via = picked, "model"
                        break
                    feedback = "\n\nYour previous answer was rejected: " + "; ".join(reasons) + ". Choose again."
                if not res.text:
                    # an unsupported 'Yes' on an experience question becomes the honest 'No' when there is one
                    res.text = no_opt.strip() if (no_opt and _EXPERIENCE_Q.search(question)) else self._fallback_choice(options)
                    res.via = "choice-fallback"
        except Exception as e:
            logger.error(f"[answer_engine] choice error for '{question}': {e}")
            res.text, res.via = self._fallback_choice(options), "choice-fallback"
        res.color = "GREEN" if (res.via == "model" and not res.missing_words) else "YELLOW"
        mem.add_turn(f"QUESTION: {question}\nOPTIONS: {'; '.join(real)}", json.dumps({"choice": res.text, "used_bullets": res.cited_ids}))
        self.results.append(res)
        return res

    # ------------------------------------------------ numbers
    def _profile_years(self) -> str:
        m = re.search(r"\d+(?:\.\d+)?", str(self.profile.get("years_experience") or self.profile.get("experience_summary") or ""))
        return m.group(0) if m else ""

    async def answer_numeric(self, question: str) -> AnswerResult:
        res = AnswerResult(text="", route="NUMERIC", question=question)
        ql = question.lower()
        try:
            terms, caps = question_terms(question)
            tech = [t for t in terms if t in TECH or t in caps]
            ledger = self.memory.ledger
            if re.search(r"salary|compensation|pay", ql) and ledger.get("salary"):
                res.text, res.via = re.sub(r"[^\d.]", "", ledger["salary"]), "rule"
            elif "year" in ql and tech:
                res.text, res.via = str(years_for(self.sheet, tech)), "rule"
            elif "year" in ql and self._profile_years() and not terms:
                res.text, res.via = self._profile_years(), "rule"
            elif not self.transport.degraded:
                self.memory.flush_pending()
                user = f"QUESTION: {question}\nTASK: The form field needs a number only. Use ONLY the fact sheet and ABOUT YOU. Reply as JSON {{\"number\": <number>}}."
                r = await self.transport.chat(self.memory.view(user), 40, fmt=prompts.NUMBER_SCHEMA, temperature=0.0)
                res.calls.append({k: r[k] for k in ("secs", "ptok", "cached", "gen_tokens", "think_chars", "done")})
                obj = parse_json(r["content"], key="number")
                if obj is not None:
                    n = obj["number"]
                    s = str(int(n)) if float(n).is_integer() else str(n)
                    known = self.sheet.text() + " " + self.sheet.p1 + " " + " ".join(ledger.values())
                    if s in re.sub(r",", "", known):
                        res.text, res.via = s, "model"
        except Exception as e:
            logger.error(f"[answer_engine] numeric error for '{question}': {e}")
        if not res.text:
            res.text, res.via = "0", "numeric-fallback"      # the field needs a number; 0 is never claimed as experience
        res.color = "GREEN" if res.via in ("rule", "model") else "YELLOW"
        self.results.append(res)
        return res

    # ------------------------------------------------ other calls through the same cached prefix (not stored in the chat)
    async def side_call(self, task: str, schema: dict | None = None, max_tokens: int = 300) -> str:
        self.memory.flush_pending()
        r = await self.transport.chat(self.memory.view(task), max_tokens, fmt=schema)
        return r["content"]

    # ------------------------------------------------ audit
    def save_audit(self, log_dir: str) -> None:
        if not self.settings.get("audit", True):
            return
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "fact_sheet.txt"), "w", encoding="utf-8") as fh:
                fh.write(f"source={self.sheet.source}\n\n{self.sheet.p1}\n\n{self.sheet.render()}\n")
            with open(os.path.join(log_dir, "answers.jsonl"), "w", encoding="utf-8") as fh:
                for r in self.results:
                    fh.write(json.dumps(r.__dict__, ensure_ascii=False, default=str) + "\n")
                for ev in self.memory.events:
                    fh.write(json.dumps({"trim_event": ev}) + "\n")
        except Exception as e:
            logger.error(f"[answer_engine] failed to save audit files: {e}")
