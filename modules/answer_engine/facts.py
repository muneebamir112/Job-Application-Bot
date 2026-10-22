"""Fact sheet: the numbered, never-rewritten facts about the candidate that the AI may cite.

Sources, in priority order (ANSWER_FACTS_SOURCE=auto):
  1. structured tailored-resume JSON saved by resume-bot  (json)
  2. text of the resume PDF the bot uploads                (pdf)
  3. structured data inside the profile JSON               (profile)
  4. nothing found -> P1 only ("no-facts mode")
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from .lexicons import stem, wt

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass
class FactSheet:
    facts: list = field(default_factory=list)       # [{"id","kind","text"}]
    p1: str = ""
    source: str = "none"
    index: dict = field(default_factory=dict)

    def __post_init__(self):
        self.reindex()

    def reindex(self):
        self.index = {f["id"]: f for f in self.facts}
        self.index["P1"] = {"id": "P1", "kind": "P", "text": self.p1}

    @property
    def employers(self) -> list[str]:
        names = {fact_parts(f)[0] for f in self.facts if f["kind"] == "W"} - {None}
        return sorted(names, key=len, reverse=True)

    @property
    def has_facts(self) -> bool:
        return any(f["kind"] in ("W", "S") for f in self.facts)

    def text(self) -> str:
        return " ".join(f["text"] for f in self.facts)

    def render(self) -> str:
        out, last = [], None
        names = {"W": "WORK HISTORY", "S": "SKILLS", "E": "EDUCATION", "M": "SUMMARY (as written in the resume)"}
        for f in self.facts:
            if f["kind"] != last:
                out += ["", names[f["kind"]]]
                last = f["kind"]
            out.append(f"[{f['id']}] {f['text']}")
        return "\n".join(out).strip()


def fact_parts(f: dict):
    """(employer, body) of a work fact; (None, text) for other kinds."""
    if f["kind"] != "W" or "): " not in f["text"]:
        return None, f["text"]
    head, body = f["text"].split("): ", 1)
    return head.split(" - ")[0], body


# ---------------------------------------------------------------- P1
def _p1(profile: dict) -> str:
    name = profile.get("full_name") or profile.get("name")
    loc = profile.get("location") or profile.get("address")
    yrs = profile.get("years_experience") or profile.get("experience_summary") or ""
    return f"[P1] Name: {name}. Location: {loc}. Total experience: {yrs}. Email: {profile.get('email')}. Phone: {profile.get('phone')}."


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


# ---------------------------------------------------------------- 1. structured resume JSON
def _json_path(data_dir: str, company: str, profile_name: str) -> str | None:
    if not (data_dir and company and profile_name):
        return None
    prof = profile_name.lower().replace(" ", "_")
    names = {company.lower().replace(" ", "_"), re.sub(r"[^a-z0-9._ -]", "", company.lower()).strip().replace(" ", "_")}
    for n in names:
        p = os.path.join(data_dir, f"{n}_{prof}_ollama.json")
        if os.path.isfile(p):
            return p
    return None


def facts_from_resume_json(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    facts = []
    for j, job in enumerate(d.get("experience") or [], 1):
        for b, t in enumerate(job.get("bullets") or [], 1):
            facts.append({"id": f"W{j}.{b}", "kind": "W", "text": f"{job.get('company')} - {job.get('title')} ({job.get('dates')}): {t}"})
    for n, row in enumerate(d.get("skills") or [], 1):
        if isinstance(row, (list, tuple)) and len(row) == 2:
            items = ", ".join(row[1]) if isinstance(row[1], (list, tuple)) else str(row[1])
            facts.append({"id": f"S{n}", "kind": "S", "text": f"{row[0]}: {items}"})
        else:
            facts.append({"id": f"S{n}", "kind": "S", "text": str(row)})
    edu = d.get("education")
    edus = edu if isinstance(edu, list) else ([edu] if edu else [])
    for n, e in enumerate(edus, 1):
        text = f"{e.get('school', '')} - {e.get('degree_line', '')}" if isinstance(e, dict) else str(e)
        facts.append({"id": f"E{n}", "kind": "E", "text": text.strip(" -")})
    for n, s in enumerate(_sentences(d.get("summary") or ""), 1):
        facts.append({"id": f"M{n}", "kind": "M", "text": s})
    return facts


# ---------------------------------------------------------------- 2. resume PDF text
def _is_company_line(l: str) -> bool:
    return "|" in l and re.search(r"\b(19|20)\d\d\b|present", l, re.I) and not l.lstrip().startswith(("•", "-", "*"))


def facts_from_resume_text(resume_text: str) -> list[dict]:
    text = (resume_text or "").replace("(cid:127)", "•")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    sec_re = [("summary", r"^(professional\s+)?summary$"), ("work", r"^(work|professional)\s+experience$|^experience$"),
              ("skills", r"^(technical\s+)?skills$"), ("education", r"^education$"), ("keywords", r"^keywords$")]
    sections, cur = {}, None
    for l in lines:
        hit = next((k for k, rx in sec_re if re.match(rx, l, re.I)), None)
        if hit:
            cur = hit
            sections[cur] = []
            continue
        if cur:
            sections[cur].append(l)
    facts, job, jobs, i = [], None, [], 0
    work = sections.get("work", [])
    while i < len(work):
        l = work[i]
        nxt = work[i + 1] if i + 1 < len(work) else ""
        if not l.startswith("•") and _is_company_line(nxt) and not _is_company_line(l):
            parts = [p.strip() for p in nxt.split("|")]
            job = {"title": l, "company": parts[0], "dates": parts[-1], "bullets": []}
            jobs.append(job)
            i += 2
            continue
        if l.startswith("•"):
            if job is not None:
                job["bullets"].append(l.lstrip("• ").strip())
        elif job is not None and job["bullets"]:
            job["bullets"][-1] += " " + l
        i += 1
    for j, jb in enumerate(jobs, 1):
        for b, t in enumerate(jb["bullets"], 1):
            facts.append({"id": f"W{j}.{b}", "kind": "W", "text": f"{jb['company']} - {jb['title']} ({jb['dates']}): {t}"})
    for n, l in enumerate(sections.get("skills", []), 1):
        facts.append({"id": f"S{n}", "kind": "S", "text": l.replace("_", " ")})
    for n, l in enumerate(sections.get("education", []), 1):
        facts.append({"id": f"E{n}", "kind": "E", "text": l})
    for n, s in enumerate(_sentences(" ".join(sections.get("summary", []))), 1):
        facts.append({"id": f"M{n}", "kind": "M", "text": s})
    return facts


# ---------------------------------------------------------------- 3. profile JSON
def facts_from_profile(profile: dict) -> list[dict]:
    facts = []
    for j, job in enumerate(profile.get("work_experience") or [], 1):
        if not isinstance(job, dict):
            continue
        dates = f"{job.get('start', '')} – {job.get('end', '')}".strip(" –")
        for b, t in enumerate(_sentences(job.get("summary") or ""), 1):
            facts.append({"id": f"W{j}.{b}", "kind": "W", "text": f"{job.get('company')} - {job.get('title')} ({dates}): {t}"})
    skills = [str(s) for s in (profile.get("skills") or []) if s]
    for n in range(0, len(skills), 10):
        facts.append({"id": f"S{n // 10 + 1}", "kind": "S", "text": "Skills: " + ", ".join(skills[n:n + 10])})
    edu = profile.get("education")
    for n, e in enumerate(edu if isinstance(edu, list) else ([edu] if edu else []), 1):
        facts.append({"id": f"E{n}", "kind": "E", "text": e if isinstance(e, str) else ", ".join(f"{v}" for v in e.values() if v)})
    return facts


# ---------------------------------------------------------------- entry point
def build_fact_sheet(profile: dict, resume_text: str, company: str, profile_name: str, source: str = "auto", data_dir: str = "") -> FactSheet:
    p1 = _p1(profile)
    order = ["json", "pdf", "profile"] if source == "auto" else [source]
    for src in order:
        facts = []
        try:
            if src == "json":
                path = _json_path(data_dir, company, profile_name)
                facts = facts_from_resume_json(path) if path else []
            elif src == "pdf":
                facts = facts_from_resume_text(resume_text)
            elif src == "profile":
                facts = facts_from_profile(profile)
        except Exception:
            facts = []
        if any(f["kind"] == "W" for f in facts):
            return FactSheet(facts, p1, src)
    return FactSheet([], p1, "none")


# ---------------------------------------------------------------- years from dates (numeric questions)
def _parse_date(s: str, end: bool):
    s = (s or "").strip().lower()
    if s in ("present", "current", "now", "today"):
        n = datetime.now()
        return n.year * 12 + n.month
    m = re.search(r"(?:([a-z]{3})[a-z]*\.?\s+)?((?:19|20)\d\d)", s)
    if not m:
        return None
    mon = _MONTHS.get((m.group(1) or "")[:3], 12 if end else 1)
    return int(m.group(2)) * 12 + mon


def years_for(sheet: FactSheet, term_words: list[str]) -> int:
    """Whole years: sum of date spans of jobs whose work facts mention the term; 1 if only in skills; 0 if nowhere."""
    want = {stem(w) for w in term_words if w}
    if not want:
        return 0
    spans, in_skills = {}, False
    for f in sheet.facts:
        low = {stem(t) for t in wt(f["text"])}
        if not (want & low):
            continue
        if f["kind"] == "W":
            m = re.search(r"\(([^)]*)\):", f["text"])
            if m:
                parts = re.split(r"\s*[–—-]\s*", m.group(1))
                if len(parts) >= 2:
                    a, b = _parse_date(parts[0], False), _parse_date(parts[-1], True)
                    if a and b and b >= a:
                        spans[f["text"].split("):")[0]] = b - a
        elif f["kind"] == "S":
            in_skills = True
    months = sum(spans.values())
    if months:
        return max(1, round(months / 12))
    return 1 if in_skills else 0
