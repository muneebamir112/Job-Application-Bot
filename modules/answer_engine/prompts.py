"""Prompt texts and the JSON reply schema (kept verbatim from the tested design)."""

INSTRUCTIONS = (
    "You are filling in a job application as the candidate. Write in the first person ('I', 'my').\n"
    "Facts about you come ONLY from ABOUT YOU and the FACT SHEET (each fact has an ID such as [W1.2]). "
    "Facts about the company and the role come from the JOB DESCRIPTION.\n"
    "Each question comes with a TASK. Reply as JSON: {\"answer\": \"...\", \"used_bullets\": [\"W1.2\", ...]} where used_bullets lists the IDs of the "
    "fact-sheet facts your answer is based on.\n"
    "answer: plain text, first person, at most 3 sentences, no markdown, no mention of 'the form', 'the previous question' or 'the fact sheet', "
    "and no fact IDs inside the answer text.\n"
    "Never invent employers, dates, numbers, tools, events or responsibilities. Never attach a fact from one job to another job. "
    "Never use a fact for a claim it does not state.")

TASKS = {
    "FACTUAL": ("Answer the question using ONLY facts from the FACT SHEET and list the IDs of the facts you used in used_bullets. {blocked}"
                "If the fact sheet does not directly cover the question, describe the closest real work from the fact sheet, in the first person, "
                "without claiming the thing it does not cover."),
    "BEHAVIORAL": ("Answer in the first person. If the FACT SHEET has a real example that fits, use it and list its IDs in used_bullets. {blocked}"
                   "If it has none, describe in at most 2 sentences how you generally approach this, in the present tense ('I usually...'), "
                   "with NO specific event, employer, number or tool, and used_bullets an empty list."),
    "COMPANY": ("Explain why this role and company fit you. Use the JOB DESCRIPTION only for statements about the company and the role, and list in "
                "used_bullets the IDs of the fact-sheet facts you rely on for statements about yourself."),
}

CHOICE_TASK = ("Pick exactly ONE of the options below, copied exactly as written, and put it in \"choice\". "
               "List in used_bullets the IDs of the fact-sheet facts you relied on (empty list if none apply). {blocked}"
               "If the fact sheet does not show the candidate has the experience asked about, choose the option that says no / none / 0.\nOPTIONS:\n{options}")

ANSWER_SCHEMA = {"type": "object",
                 "properties": {"answer": {"type": "string"}, "used_bullets": {"type": "array", "items": {"type": "string"}}},
                 "required": ["answer", "used_bullets"]}
CHOICE_SCHEMA = {"type": "object",
                 "properties": {"choice": {"type": "string"}, "used_bullets": {"type": "array", "items": {"type": "string"}}},
                 "required": ["choice", "used_bullets"]}
NUMBER_SCHEMA = {"type": "object", "properties": {"number": {"type": "number"}}, "required": ["number"]}


def build_system(p1: str, fact_text: str, company: str, title: str, jd: str) -> str:
    """Fixed order (instructions -> about you -> fact sheet -> job description) so Ollama can reuse its prompt cache."""
    return (INSTRUCTIONS + "\n\nABOUT YOU:\n" + p1 + "\n\nFACT SHEET:\n" + fact_text +
            f"\n\nJOB DESCRIPTION ({company}, {title}):\n{jd}")


def user_message(question: str, mode: str, missing: list[str], feedback: str = "") -> str:
    blocked = f"The fact sheet says nothing about: {', '.join(missing)}. Do not claim or imply it. " if missing else ""
    return f"QUESTION: {question}\nTASK: {TASKS[mode].format(blocked=blocked)}{feedback}"


def retry_feedback(previous: str, reasons: list[str]) -> str:
    return ("\n\nYour previous answer was rejected.\nPrevious answer: " + previous + "\nProblems:\n- " + "\n- ".join(reasons)[:700] +
            "\nWrite a corrected answer that fixes these problems.")
