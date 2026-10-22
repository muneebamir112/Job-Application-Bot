"""Routing (cheap code, no AI): which kind of question is this, and which question words does the resume not cover?"""
import re

from .lexicons import (DEMOGRAPHIC_RX, ASK_VERB, BEHAV, COMPANY_RX, EMPLOYED_BEFORE, INTENT_GENERIC, LEDGER_TOPICS, META, RISKY_STEMS, STOP, TECH, URL_ASK, URL_SITE,
                       hit, stem, tokens, wstems)


def question_terms(question: str):
    """(stemmed content words, capitalised stems) of a question."""
    orig = re.findall(r"[A-Za-z][A-Za-z0-9+#./-]*", question)
    caps = {stem(w) for i, w in enumerate(orig) if i > 0 and w[0].isupper() and not w.isupper() or (w.isupper() and len(w) > 1)}
    out = []
    for t in tokens(question):
        if t in STOP or t in META:
            continue
        s = stem(t)
        if s in INTENT_GENERIC or t in INTENT_GENERIC:
            continue
        if len(s) < 3 and s not in ("go", "c#", "c++", "ai", "ml"):
            continue
        out.append(s)
    return list(dict.fromkeys(out)), caps


class Router:
    def __init__(self, fact_text: str, company: str):
        self.fact_stems = wstems(fact_text)
        self.fact_low = fact_text.lower()
        self.company_stems = {stem(t) for t in tokens(company)}

    def classify(self, question: str, ledger: dict):
        """-> (route, missing_words, ledger_topic)"""
        ql = question.lower()
        for name, rx in LEDGER_TOPICS:
            if re.search(rx, ql) and name in ledger:
                return "LEDGER", [], name
        if DEMOGRAPHIC_RX.search(question):
            return "DECLINE", [], None
        if EMPLOYED_BEFORE.search(question):
            return "FIXED_NO", [], None
        if URL_ASK.search(question) and ASK_VERB.search(question) and URL_SITE.search(ql):
            return "URLID", [], None
        terms, caps = question_terms(question)
        missing = [t for t in terms if (t in TECH or t in RISKY_STEMS or t in caps)
                   and t not in self.fact_stems and not hit(t, self.fact_low) and t not in self.company_stems]
        if COMPANY_RX.search(question):
            return "COMPANY", missing, None
        if BEHAV.search(question):
            return "BEHAVIORAL", missing, None
        return "FACTUAL", missing, None
