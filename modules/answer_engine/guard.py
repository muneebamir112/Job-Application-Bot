"""Guardrails: check every sentence of an AI answer against the facts it cited. Pure code, no AI calls."""
import re

from .facts import FactSheet, fact_parts
from .lexicons import (CLAIM_SENT, CLAIM_VERBS, FEELINGS, FLUFF, ID_IN_TEXT, META_REF, PAST_EVENT, RISKY_STEMS, STOP, TECH, hit,
                       numbers, sentences, stem, word_clean, wstems, wt)


def clean_answer(ans: str) -> str:
    """Soft fixes: drop fact IDs / markdown / sentences that talk about 'facts', tidy leftovers, keep at most 3 sentences."""
    a = ID_IN_TEXT.sub("", ans or "")
    a = re.sub(r"[*#`]", "", a)
    a = re.sub(r"\s+", " ", a).strip()
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", a) if s.strip()]
    sents = [s for s in sents if not re.search(r"\b(facts?|cited|used_bullets)\b", s, re.I)]
    a = " ".join(sents[:3]).strip()
    a = re.sub(r"\(\s*\)", "", a)
    a = re.sub(r"\b(as|which is|this is)\s+(highlighted|shown|evidenced|demonstrated|stated|described|noted|seen)\s+(in|by)\s*(?=[,.;)]|$)", "", a, flags=re.I)
    a = re.sub(r"\s+([,.;:])", r"\1", a)
    a = re.sub(r",\s*,+", ",", a)
    return re.sub(r"\s{2,}", " ", a).strip()


def clean_ids(ids) -> list[str]:
    return [re.sub(r"[\[\]\s]", "", str(i)) for i in (ids or [])]


def content_stems(s: str) -> set[str]:
    return {stem(t) for t in wt(s) if t not in STOP and len(t) > 2}


class Guard:
    def __init__(self, sheet: FactSheet, company: str, jd: str):
        self.sheet, self.company, self.jd = sheet, company, jd
        self.employers = sheet.employers
        self.jd_stems = wstems(jd)

    def check(self, question: str, mode: str, missing: list[str], obj: dict):
        """-> (global_problems, [(sentence, [problems])], cited_ids). obj["answer"]/["used_bullets"] are cleaned in place."""
        ans = clean_answer(obj.get("answer") or "")
        used = clean_ids(obj.get("used_bullets"))
        obj["answer"], obj["used_bullets"] = ans, used
        if not ans:
            return ["empty answer"], [], used
        F = self.sheet.index
        glob, per = [], []
        bad_ids = [i for i in used if i not in F]
        if bad_ids:
            glob.append(f"you cited IDs that are not in the fact sheet: {bad_ids}")
        cited = [F[i] for i in used if i in F]
        approach_only = mode == "BEHAVIORAL" and not cited
        if mode == "FACTUAL" and not cited:
            glob.append("cite at least one fact ID in used_bullets")
        if not re.search(r"\b(I|my|me|I'm|I've)\b", ans, re.I):
            glob.append("not written in the first person")
        if re.search(r"\b(the candidate|they|their)\b", ans, re.I):
            glob.append("written in the third person")
        if META_REF.search(ans):
            glob.append("refers to the form or the fact sheet")
        blocked_all = set(missing)
        for s in sentences(ans):
            why, low = [], s.lower()
            E_s = [e for e in self.employers if e.lower() in low]
            if approach_only:
                pool_facts = []
            elif E_s:
                pool_facts = [f for f in cited if fact_parts(f)[0] in E_s]
                if not pool_facts:
                    why.append(f"you mention {', '.join(E_s)} but cited no fact from that job")
            else:
                pool_facts = cited
            pool = " ".join(f["text"] for f in pool_facts)
            pool_stems, pool_toks = wstems(pool), set(wt(pool))
            bad_nums = [n for n in numbers(s) if n not in numbers(pool)]
            if bad_nums:
                why.append(f"these numbers are not in the cited facts for this sentence: {sorted(bad_nums)}")
            extra = (" " + self.jd) if mode == "COMPANY" else ""
            allowed = wstems(pool + " " + self.company + extra)
            ents = []
            words = [word_clean(w) for w in re.findall(r"[A-Za-z][A-Za-z0-9+#]*", s)]
            for i, w in enumerate(words):
                if w and i and w.lower() not in STOP and (w[0].isupper() or (w.isupper() and len(w) > 1) or stem(w) in TECH) and stem(w) not in allowed:
                    ents.append(w)
            if ents:
                why.append(f"these names/tools are not in the cited facts for this sentence: {sorted(set(ents))}")
            pool_low = pool.lower()
            risky = [r for r in (RISKY_STEMS | blocked_all) if hit(r, low) and not hit(r, pool_low)]
            if re.search(r"open[- ]source", low) and "source" not in pool_stems:
                risky.append("open source")
            if risky:
                why.append(f"you claimed something the cited facts do not state: {sorted(set(risky))}")
            cv = [v for v in (CLAIM_VERBS & set(wt(s))) if v not in pool_toks]
            if cv:
                why.append(f"the cited facts do not say you {cv}")
            if approach_only:
                if numbers(s):
                    why.append("approach answers may not contain numbers")
                if PAST_EVENT.search(s):
                    why.append("describe how you usually work, not a past event")
            if mode == "COMPANY":
                cs = content_stems(s)
                if len(cs) >= 4:
                    all_ok = {stem(t) for t in wt(" ".join(f["text"] for f in cited))} | self.jd_stems | wstems(self.company + " " + question) | FEELINGS
                    if len(cs & all_ok) / len(cs) < 0.6:
                        why.append("this statement is not supported by the job description or the cited facts")
            elif CLAIM_SENT.search(s) and not approach_only:
                cs = content_stems(s)
                if len(cs) >= 4:
                    sup = {x for x in cs if x in pool_stems or x in allowed or x in FLUFF}
                    if len(sup) / len(cs) < 0.4:
                        why.append("this sentence is not supported by the cited facts")
            per.append((s, why))
        return glob, per, used
