"""Benchmark the answer engine on saved application questions, one column of answers per model.

Reads the jobs sheet (and the question lists) from an Excel workbook, replays every question the way the bot would
(profile step first, then the v2 answer engine) with each model, and writes the results back into the SAME workbook:
    Benchmark          one row per question, one answer column per model
    Benchmark Details  one row per question per model (answered by, colour, route, cited facts, seconds, tokens, rejected attempts)
    Benchmark Summary  one row per model (green/yellow, blanks, fallbacks, speed)
No browser is opened and nothing is written to Sheet3. Your .env is never changed: the model is overridden per run.

Every question goes straight to the v2 answer engine (no bot form rules, no browser) with the chosen model.\n\nUsage (from the Job-Bot folder):
    python benchmark/run_benchmark.py --excel "C:/path/Jobs.xlsx" --profile "Brian Moore" --models qwen3:1.7b,qwen2.5:3b
Options: --jobs-sheet Jobs  --docs-dir <folder with the saved question docs>  --think  --force  --jobs 1,2,5  --keep-loaded
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # Job-Bot/
sys.path.insert(0, ROOT)

import requests                                                          # noqa: E402
from openpyxl import Workbook, load_workbook                             # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill                 # noqa: E402
from openpyxl.utils import get_column_letter                             # noqa: E402

import config                                                            # noqa: E402
from modules.answer_engine import AnswerEngine, engine_settings          # noqa: E402
from modules.resume_parser import extract_text_from_pdf                  # noqa: E402

SHEETS = ("Benchmark", "Benchmark Details", "Benchmark Summary")
DETAIL_COLS = ["Job ID", "Company", "Job Title", "Q#", "Question", "Model", "Answer", "Answered by", "Colour", "Route", "How (ladder step)",
               "Missing words", "Cited facts", "Seconds", "Model calls", "Prompt tokens", "Cached tokens", "Rejected attempts"]


# ------------------------------------------------------------------ reading the workbook
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def read_table(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    head = [str(h or "").strip() for h in rows[0]]
    return [{head[i]: r[i] for i in range(len(head)) if head[i]} for r in rows[1:] if any(c not in (None, "") for c in r)]


def pick(row: dict, *names):
    wanted = {_norm(n) for n in names}
    for k, v in row.items():
        if _norm(k) in wanted and v not in (None, ""):
            return v
    return None


def parse_questions(lines: list[str]) -> list[str]:
    """Numbered question lines after the 'Application Form Questions' heading (or all numbered lines)."""
    out, on = [], False
    has_heading = any(re.search(r"application form questions", l, re.I) for l in lines)
    for raw in lines:
        l = raw.strip()
        if not l:
            continue
        if re.search(r"application form questions", l, re.I):
            on = True
            continue
        if has_heading and not on:
            continue
        if re.match(r"^(company|job title|application url|total form questions)\b", l, re.I):
            continue
        q = re.sub(r"^\s*(?:\d+[.)]|[-•*])\s*", "", l).strip()
        if len(q) > 3:
            out.append(q)
    return out


def read_question_file(path: str) -> list[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        from docx import Document
        return parse_questions([p.text for p in Document(path).paragraphs])
    if ext in (".txt", ".md", ".csv"):
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return parse_questions(fh.read().splitlines())
    if ext == ".pdf":
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return parse_questions("\n".join((pg.extract_text() or "") for pg in pdf.pages).splitlines())
    return []


def find_question_file(job: dict, docs_dir: str, excel_dir: str) -> str | None:
    explicit = pick(job, "Questions File", "Questions Path")
    for base in (None, excel_dir, docs_dir):
        if explicit:
            p = explicit if base is None else os.path.join(base, str(explicit))
            if os.path.isfile(p):
                return p
    if not docs_dir or not os.path.isdir(docs_dir):
        return None
    jid, comp = str(job["id"]), _norm(job["company"])
    files = [f for f in os.listdir(docs_dir) if os.path.splitext(f)[1].lower() in (".docx", ".txt", ".md", ".pdf")]
    for f in files:                                           # file named after the job number: "1.docx", "1 - ....docx", "job_1.txt"
        if re.match(rf"^(job[ _-]*)?0*{re.escape(jid)}\b", f, re.I):
            return os.path.join(docs_dir, f)
    for f in files:                                           # or containing company (and title when several match)
        if comp and comp in _norm(f) and _norm(job["title"]) in _norm(f):
            return os.path.join(docs_dir, f)
    for f in files:
        if comp and comp in _norm(f):
            return os.path.join(docs_dir, f)
    return None


def load_jobs(wb, args, excel_dir: str) -> list[dict]:
    ws = wb[args.jobs_sheet]
    questions_sheet = {}
    if "Questions" in wb.sheetnames:                          # optional: columns "Job ID" / "Question"
        for r in read_table(wb["Questions"]):
            jid, q = pick(r, "Job ID", "No", "No."), pick(r, "Question")
            if jid is not None and q:
                key = str(int(jid)) if isinstance(jid, (int, float)) else str(jid).strip()
                questions_sheet.setdefault(key, []).append(str(q).strip())
    jobs = []
    for n, r in enumerate(read_table(ws), 1):
        jid = pick(r, "No.", "No", "Job ID", "ID")
        jid = str(int(jid)) if isinstance(jid, (int, float)) else str(jid or n)
        job = {"id": jid, "company": str(pick(r, "Company Name", "Company") or "").strip(), "title": str(pick(r, "Job Title", "Title") or "").strip(),
               "jd": str(pick(r, "Job Description", "Description") or ""), "profile": str(pick(r, "Profile", "Profile Name") or args.profile or "").strip(),
               "resume": pick(r, "Resume", "Resume Path", "Resume File"), "_row": r}
        qfile = find_question_file(job, args.docs_dir, excel_dir)
        job["questions"] = questions_sheet.get(jid) or (read_question_file(qfile) if qfile else [])
        job["questions_source"] = "Questions sheet" if questions_sheet.get(jid) else (qfile or "NOT FOUND")
        jobs.append(job)
    if args.jobs:
        keep = {j.strip() for j in args.jobs.split(",")}
        jobs = [j for j in jobs if j["id"] in keep]
    return jobs


# ------------------------------------------------------------------ profile + resume
def load_profile(name: str) -> dict:
    for p in (os.path.join(ROOT, "profiles", f"{name}.json"), os.path.join(ROOT, "profiles", f"{name.lower().replace(' ', '_')}.json")):
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    raise SystemExit(f"Profile '{name}' not found in {os.path.join(ROOT, 'profiles')}. Pass --profile or add a Profile column.")


def resolve_resume(job: dict, excel_dir: str) -> str | None:
    cands = []
    if job["resume"]:
        r = str(job["resume"])
        cands += [r, os.path.join(excel_dir, r), os.path.join(config.CVS_DIR, r)]
    comp, prof = job["company"], job["profile"]
    cands += [os.path.join(config.CVS_DIR, f"{prof} - {comp}.pdf"), os.path.join(config.CVS_DIR, f"{comp} - {prof}.pdf"),
              os.path.join(config.CVS_DIR, comp, f"{prof}.pdf")]
    for c in cands:
        if os.path.isfile(c):
            return c
    if os.path.isdir(config.CVS_DIR):                          # any PDF named with both profile and company
        for f in os.listdir(config.CVS_DIR):
            if f.lower().endswith(".pdf") and _norm(prof) in _norm(f) and _norm(comp) in _norm(f):
                return os.path.join(config.CVS_DIR, f)
    return None


# ------------------------------------------------------------------ one application with one model
async def run_job(job: dict, model: str, args, excel_dir: str, audits_dir: str) -> list[dict]:
    profile = dict(load_profile(job["profile"]))
    resume_path = resolve_resume(job, excel_dir)
    resume_text = ""
    if resume_path:
        try:
            resume_text = extract_text_from_pdf(resume_path)
        except Exception as e:
            print(f"   ! could not read resume {resume_path}: {e}")
    else:
        print(f"   ! no resume found for job {job['id']} ({job['company']}); engine will use the profile data only")
    profile.update({"resume_file_path": resume_path, "resume_text": resume_text, "job_title": job["title"],
                    "company_name": job["company"], "job_description": job["jd"]})
    settings = engine_settings()
    settings["model"], settings["think"] = model, bool(args.think)
    if args.think:
        settings["num_predict"] = max(settings["num_predict"], 800)
    engine = await AnswerEngine.create(profile, resume_text, job["company"], job["title"], job["jd"], job["profile"], settings)
    rows = []
    for n, q in enumerate(job["questions"], 1):
        t0 = time.time()
        row = {"Job ID": job["id"], "Company": job["company"], "Job Title": job["title"], "Q#": n, "Question": q, "Model": model}
        r = await engine.answer_open(q)
        attempts = [f"#{a['attempt']}: {a['answer'][:160]} -> {'; '.join(a['reasons'])[:300]}" for a in r.attempts if a["reasons"]]
        row.update({"Answer": r.text, "Answered by": "v2 (rule)" if r.via in ("rule", "no-data") else "v2 (AI)", "Colour": r.color, "Route": r.route,
                    "How (ladder step)": r.via, "Missing words": ", ".join(r.missing_words), "Cited facts": ", ".join(r.cited_ids),
                    "Seconds": round(time.time() - t0, 1), "Model calls": len(r.calls), "Prompt tokens": sum(c["ptok"] for c in r.calls),
                    "Cached tokens": sum(c["cached"] for c in r.calls), "Rejected attempts": "\n".join(attempts)})
        rows.append(row)
        print(f"   Q{n} [{row['Answered by']}/{row['Colour']}/{row['How (ladder step)']}] {q[:60]!r} -> {row['Answer'][:70]!r}")
    engine.save_audit(os.path.join(audits_dir, re.sub(r"[^A-Za-z0-9._-]", "_", model), f"job_{job['id']}"))
    return rows


# ------------------------------------------------------------------ models
def installed_models() -> set[str]:
    try:
        r = requests.get(f"{config.OLLAMA_HOST.rstrip('/')}/api/tags", timeout=10)
        return {m["name"] for m in r.json().get("models", [])}
    except Exception as e:
        raise SystemExit(f"Cannot reach Ollama at {config.OLLAMA_HOST}: {e}")


def unload(model: str) -> None:
    try:
        requests.post(f"{config.OLLAMA_HOST.rstrip('/')}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


# ------------------------------------------------------------------ writing results
def summarize(rows: list[dict]) -> dict:
    ai = [r for r in rows if r["Answered by"] == "v2 (AI)"]
    ptok, cached = sum(r["Prompt tokens"] for r in ai), sum(r["Cached tokens"] for r in ai)
    secs = [r["Seconds"] for r in ai]
    return {"Questions": len(rows), "Answered by engine code rules": sum(r["Answered by"] == "v2 (rule)" for r in rows), "Answered by AI": len(ai),
            "Green": sum(r["Colour"] == "GREEN" for r in rows), "Yellow": sum(r["Colour"] == "YELLOW" for r in rows),
            "Blank answers": sum(not str(r["Answer"]).strip() for r in rows),
            "AI answers accepted as written": sum(r["How (ladder step)"] == "model" for r in ai),
            "AI answers that needed a fallback (salvage/restate/summary)": sum(r["How (ladder step)"] in ("salvage", "restate", "summary") for r in ai),
            "Avg seconds per AI question": round(sum(secs) / len(secs), 1) if secs else 0, "Total AI seconds": round(sum(secs), 1),
            "Avg model calls per AI question": round(sum(r["Model calls"] for r in ai) / len(ai), 2) if ai else 0,
            "Prompt read from cache %": round(100 * cached / ptok) if ptok else 0}


def write_sheets(wb, cache: dict, jobs: list[dict], models: list[str]) -> None:
    for name in SHEETS:
        if name in wb.sheetnames:
            del wb[name]
    wrap = Alignment(wrap_text=True, vertical="top")
    bold = Font(bold=True)
    fills = {"GREEN": PatternFill("solid", fgColor="D9EAD3"), "YELLOW": PatternFill("solid", fgColor="FFF2CC")}

    # ---- Benchmark: one answer column per model
    ws = wb.create_sheet("Benchmark")
    ws.append(["Job ID", "Company", "Job Title", "Job Description", "Q#", "Question"] + models)
    by = {m: {(r["Job ID"], r["Q#"]): r for r in cache.get(m, [])} for m in models}
    for job in jobs:
        for n, q in enumerate(job["questions"], 1):
            first = n == 1
            ws.append([job["id"], job["company"] if first else "", job["title"] if first else "", job["jd"] if first else "", n, q] +
                      [by[m].get((job["id"], n), {}).get("Answer", "") for m in models])
            for c, m in enumerate(models, 7):
                col = by[m].get((job["id"], n), {}).get("Colour")
                if col in fills:
                    ws.cell(ws.max_row, c).fill = fills[col]
        if not job["questions"]:
            ws.append([job["id"], job["company"], job["title"], job["jd"], "", f"(no questions found: {job['questions_source']})"])
    for c in ws[1]:
        c.font = bold
    for i, w in enumerate([8, 18, 24, 40, 5, 55] + [60] * len(models), 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = wrap
        if len(str(row[3].value or "")) > 300:
            ws.row_dimensions[row[0].row].height = 150
    ws.freeze_panes = "G2"

    # ---- Details: long form
    wd = wb.create_sheet("Benchmark Details")
    wd.append(DETAIL_COLS)
    for m in models:
        for r in cache.get(m, []):
            wd.append([r.get(c, "") for c in DETAIL_COLS])
            fill = fills.get(r.get("Colour"))
            if fill:
                wd.cell(wd.max_row, DETAIL_COLS.index("Colour") + 1).fill = fill
    for c in wd[1]:
        c.font = bold
    for i, w in enumerate([8, 16, 22, 5, 45, 18, 60, 12, 9, 14, 14, 18, 18, 9, 8, 9, 9, 60], 1):
        wd.column_dimensions[get_column_letter(i)].width = w
    for row in wd.iter_rows(min_row=2):
        for c in row:
            c.alignment = wrap
    wd.freeze_panes = "A2"
    wd.auto_filter.ref = wd.dimensions

    # ---- Summary: one row per model
    wsu = wb.create_sheet("Benchmark Summary")
    stats = {m: summarize(cache.get(m, [])) for m in models if cache.get(m)}
    if stats:
        keys = list(next(iter(stats.values())).keys())
        wsu.append(["Model"] + keys)
        for m, s in stats.items():
            wsu.append([m] + [s[k] for k in keys])
        for c in wsu[1]:
            c.font, c.alignment = bold, wrap
        wsu.column_dimensions["A"].width = 24
        for i in range(2, len(keys) + 2):
            wsu.column_dimensions[get_column_letter(i)].width = 16
        wsu.row_dimensions[1].height = 75


def save_workbook(wb, path: str) -> str:
    try:
        wb.save(path)
        return path
    except PermissionError:
        alt = os.path.splitext(path)[0] + "_benchmark_copy.xlsx"
        wb.save(alt)
        print(f"   ! '{path}' is open in another program; saved to {alt} instead")
        return alt


# ------------------------------------------------------------------ main
async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--excel", required=True)
    ap.add_argument("--models", required=True, help="comma-separated Ollama model names, e.g. qwen3:1.7b,qwen2.5:3b")
    ap.add_argument("--profile", default="", help="profile name (profiles/<name>.json) when the jobs sheet has no Profile column")
    ap.add_argument("--jobs-sheet", default="Jobs")
    ap.add_argument("--docs-dir", default="", help="folder with the saved question documents (.docx/.txt/.md/.pdf)")
    ap.add_argument("--jobs", default="", help="only these job numbers, e.g. 1,2,5")
    ap.add_argument("--think", action="store_true", help="enable model thinking")
    ap.add_argument("--force", action="store_true", help="re-run models/jobs that already have results")
    ap.add_argument("--keep-loaded", action="store_true", help="do not unload each model from memory when it finishes")
    args = ap.parse_args()

    excel = os.path.abspath(args.excel)
    excel_dir = os.path.dirname(excel)
    cache_path = os.path.splitext(excel)[0] + ".benchmark.json"
    backup = os.path.splitext(excel)[0] + ".backup.xlsx"
    if not os.path.exists(backup):
        shutil.copy2(excel, backup)
        print(f"Backed up the workbook to {backup}")
    wb = load_workbook(excel)
    if args.jobs_sheet not in wb.sheetnames:
        raise SystemExit(f"Sheet '{args.jobs_sheet}' not found. Sheets: {wb.sheetnames}")
    jobs = load_jobs(wb, args, excel_dir)
    print(f"{len(jobs)} job(s):")
    for j in jobs:
        print(f"  #{j['id']} {j['company']} - {j['title']}: {len(j['questions'])} question(s) [{j['questions_source']}] profile='{j['profile'] or '?'}'")
    if not any(j["questions"] for j in jobs):
        raise SystemExit("No questions found. Put the question docs in --docs-dir (named by job number or company) or add a 'Questions' sheet.")
    if any(not j["profile"] for j in jobs if j["questions"]):
        raise SystemExit("Some jobs have no profile. Pass --profile or add a 'Profile' column to the jobs sheet.")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    have = installed_models()
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    audits_dir = os.path.splitext(excel)[0] + "_audits"
    all_models = list(dict.fromkeys(list(cache.keys()) + models))

    for model in models:
        if model not in have and f"{model}:latest" not in have:
            print(f"\n== {model}: NOT installed in Ollama (run: ollama pull {model}) - skipped")
            continue
        done = {j for j in {r["Job ID"] for r in cache.get(model, [])}}
        if args.force:
            cache[model], done = [r for r in cache.get(model, []) if r["Job ID"] not in {j["id"] for j in jobs}], set()
        print(f"\n== model {model} ==")
        t0 = time.time()
        for job in jobs:
            if not job["questions"] or job["id"] in done:
                continue
            print(f" job #{job['id']} {job['company']} ({len(job['questions'])} questions)")
            rows = await run_job(job, model, args, excel_dir, audits_dir)
            cache.setdefault(model, []).extend(rows)
            json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)     # resumable after every job
        print(f" model {model} finished in {round(time.time() - t0)}s")
        if not args.keep_loaded:
            unload(model)
        write_sheets(wb, cache, jobs, all_models)
        out = save_workbook(wb, excel)
        print(f" results saved to {out}")
    write_sheets(wb, cache, jobs, all_models)
    print(f"\nDone. Open '{save_workbook(wb, excel)}' -> sheets: {', '.join(SHEETS)}")


if __name__ == "__main__":
    asyncio.run(main())
