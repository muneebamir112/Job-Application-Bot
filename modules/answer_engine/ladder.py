"""The failure ladder. A field is never left blank:
   1 attempt  ->  2 retry (shown the rejection)  ->  3 salvage passing sentences  ->  4 restate a real resume line  ->  5 profile summary."""
import json
import re
import time

from . import prompts
from .facts import fact_parts
from .lexicons import STOP, stem, wt
from .result import AnswerResult
from .router import question_terms
from .transport import TransportError, parse_json


# ---------------------------------------------------------------- deterministic fallbacks (never invented)
def summary_sentence(engine) -> str:
    p = engine.profile
    yrs = p.get("years_experience") or p.get("experience_summary") or ""
    sk = next((f["text"] for f in engine.sheet.facts if f["kind"] == "S"), "")
    items = [x.strip() for x in sk.split(":", 1)[-1].split(",") if x.strip()][:4]
    yrs = re.sub(r"\s*experience\s*$", "", str(yrs), flags=re.I).strip()
    if not yrs:
        return "I am an experienced software engineer."
    return f"I am a software engineer with {yrs} of experience" + (f", working mainly with {', '.join(items)}." if items else ".")


def _jd_mission_sentence(engine) -> str:
    sents = [s.strip(" -•\t") for s in re.split(r"(?<=[.!?])\s+|\n+", engine.jd or "")]
    sents = [s for s in sents if len(s) >= 30]
    keys = {"mission", "problem", "solv", "platform", "grow", "customer", "team", "build"}
    if not sents:
        return ""
    return max(sents, key=lambda s: len(keys & {stem(t) for t in wt(s)}))


def restate_line(engine, question: str, mode: str, cited_ids: list[str]) -> str:
    if mode == "COMPANY":
        mission = _jd_mission_sentence(engine)
        lead = f"What draws me to {engine.company}: {mission.rstrip('.')}. " if mission else ""
        return lead + summary_sentence(engine)
    F = engine.sheet.index
    order = [F[i] for i in cited_ids if i in F and F[i]["kind"] == "W"]
    if not order:
        terms, _ = question_terms(question)
        want = set(terms)
        scored = sorted(((len(want & {stem(t) for t in wt(f["text"]) if t not in STOP}), n, f)
                         for n, f in enumerate(engine.sheet.facts) if f["kind"] == "W"), key=lambda x: (-x[0], x[1]))
        order = [f for sc, _, f in scored if sc > 0][:3] or [f for f in engine.sheet.facts if f["kind"] == "W" and re.search(r"\d", f["text"])][:1]
    for f in order:
        emp, body = fact_parts(f)
        first = body.split()[0] if body.split() else ""
        if not (first.endswith("ed") or first in ("Led", "Built", "Ran", "Wrote")):
            continue
        return f"At {emp}, I {body[0].lower() + body[1:]}".replace(" our ", " the ")
    return ""


def link_answer(engine, question: str) -> str:
    ql, p = question.lower(), engine.profile
    for key, words in (("github", ("github",)), ("linkedin", ("linkedin",)), ("portfolio", ("portfolio", "website", "web site")), ("twitter", ("twitter",))):
        if any(w in ql for w in words):
            return p.get(key) or "N/A"
    return "N/A"


def ledger_answer(engine, topic: str) -> str:
    v = engine.memory.ledger.get(topic, "")
    if topic == "authorization":
        return "Yes, I am legally authorized to work in the United States." if str(v).lower().startswith("y") else "No, I am not authorized to work in the United States without sponsorship."
    if topic == "sponsorship":
        return "No, I do not require visa sponsorship now or in the future." if str(v).lower().startswith("n") else "Yes, I will require visa sponsorship."
    return f"My desired salary is {v}."


# ---------------------------------------------------------------- the ladder
async def answer_open(engine, question: str) -> AnswerResult:
    mem = engine.memory
    mem.turn += 1
    t0 = time.time()
    mode, missing, topic = engine.router.classify(question, mem.ledger)
    res = AnswerResult(text="", route=mode, missing_words=missing, question=question)

    if mode == "LEDGER":
        res.text, res.color, res.via = ledger_answer(engine, topic), "GREEN", "rule"
        return res
    if mode == "DECLINE":       # demographic / personal questions are never answered with invented text
        res.text, res.color, res.via = "I prefer not to answer.", "GREEN", "rule"
        return res
    if mode == "FIXED_NO":      # the candidate has never worked for the company (same as the bot's fixed 'always no' rule)
        res.text, res.color, res.via = f"No, I have not previously worked for {engine.company}.", "GREEN", "rule"
        return res
    if mode == "URLID":
        v = link_answer(engine, question)
        res.text, res.via = v, ("rule" if v != "N/A" else "no-data")
        res.color = "GREEN" if v != "N/A" else "YELLOW"
        return res

    final, feedback, last = None, "", {}
    reserve = engine.settings["num_predict"]
    if not engine.transport.degraded and engine.sheet.has_facts:
        for attempt in (1, 2)[:engine.settings["max_attempts"]]:
            user = prompts.user_message(question, mode, missing, feedback)
            try:
                mem.flush_pending()
                mem.fit(len(user), reserve)
                msgs = mem.view(user)
                r = await engine.transport.chat(msgs, reserve, fmt=prompts.ANSWER_SCHEMA)
            except TransportError as e:
                res.attempts.append({"attempt": attempt, "answer": "", "used": [], "reasons": [f"model unavailable: {e}"]})
                break
            mem.observe(r["sent_chars"], r["ptok"])
            res.calls.append({k: r[k] for k in ("secs", "ptok", "cached", "gen_tokens", "think_chars", "done")})
            obj = parse_json(r["content"])
            if obj is None:
                glob, per, used = ["reply was not valid JSON"], [], []
            else:
                glob, per, used = engine.guard.check(question, mode, missing, obj)
            reasons = glob + [f"'{s[:70]}...' -> {'; '.join(w)}" for s, w in per if w]
            shown = (obj or {}).get("answer", r["content"][:200])
            res.attempts.append({"attempt": attempt, "answer": shown, "used": used, "reasons": reasons})
            last[attempt] = (obj, glob, per, used)
            if not reasons:
                final = ("model", obj["answer"], used, user)
                break
            feedback = prompts.retry_feedback(shown, reasons)

    used_ids: list[str] = []
    if not final:
        for a in (2, 1):                                               # 3. salvage the sentences that passed
            obj, glob, per, used = last.get(a, (None, [], [], []))
            if obj is None:
                continue
            keep = [s for s, w in per if not w]
            text = " ".join(keep).strip()
            if keep and re.search(r"\b(I|my|me)\b", text) and not [g for g in glob if "cited IDs" in g]:
                final = ("salvage", text, used, None)
                break
        used_ids = [i for a in (2, 1) for i in (last.get(a, (None, [], [], []))[3] or [])]
    if not final:                                                      # 4. restate a real resume line
        text = restate_line(engine, question, mode, used_ids)
        if text:
            final = ("restate", text, used_ids[:2], None)
    if not final:                                                      # 5. profile summary
        final = ("summary", summary_sentence(engine), [], None)

    via, text, used, user = final
    res.text, res.via, res.cited_ids = text, via, used
    res.color = "YELLOW" if (missing or via != "model" or (mode == "BEHAVIORAL" and not used) or not engine.sheet.has_facts) else "GREEN"
    res.elapsed_s = round(time.time() - t0, 1)
    mem.add_turn(user or prompts.user_message(question, mode, missing), json.dumps({"answer": text, "used_bullets": used}))
    return res
