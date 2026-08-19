import os
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from xml.sax.saxutils import escape as _esc
import config
from modules.logger import logger
from modules.ollama_client import query_ollama
from modules.text_utils import strip_markdown_formatting


def _cover_letter_path(company: str) -> str:
    return os.path.join(config.CVS_DIR, company, "Cover Letter.pdf")


def generate_cover_letter_text(profile: dict, job_logger) -> str:
    """Asks Ollama for a full first-person cover letter body (salutation
    through sign-off), grounded only in the candidate's actual profile/resume."""
    job_title = profile.get("job_title") or "the role"
    company_name = profile.get("company_name") or "the company"
    full_name = profile.get("full_name") or "the candidate"
    profile_context = {k: v for k, v in profile.items() if k not in ("resume_file_path", "resume_text")}
    resume_text = (profile.get("resume_text") or "")[:4000]

    system_prompt = (
        "You write first-person cover letters for a job candidate. "
        "Use ONLY facts from the candidate profile and resume text provided - never invent employers, "
        "titles, skills, or achievements that aren't present in the context. "
        f"You are writing on behalf of '{full_name}', applying for the '{job_title}' position at "
        f"'{company_name}'. Always use those exact names - never output placeholder brackets such as "
        "[Position Title], [Company Name], or [Your Name]. "
        "Write plain prose only: no markdown, no **bold**, no headers, no bullet points."
    )
    prompt = f"""
Job title: {job_title}
Company: {company_name}
Candidate name: {full_name}

Candidate profile context:
{profile_context}

Relevant resume text:
{resume_text}

Write a complete, professional cover letter (3-4 short paragraphs) for this candidate applying to this
specific role and company. Open with "Dear Hiring Manager," reference the job title and company by name,
highlight 2-3 concrete, relevant experiences drawn only from the profile/resume above, and close with
"Sincerely," followed by the candidate's name on the next line.
Do not mention that a resume is attached. Plain prose only, no markdown:
"""
    job_logger.info("Generating cover letter via Ollama...")
    text = query_ollama(prompt, system_prompt=system_prompt, timeout=config.OLLAMA_LONG_TIMEOUT)
    return strip_markdown_formatting(text)


def save_cover_letter_pdf(text: str, output_path: str) -> None:
    """Renders the cover letter body as a plain business-letter-style PDF."""
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        topMargin=1 * inch, bottomMargin=1 * inch,
        leftMargin=1 * inch, rightMargin=1 * inch,
    )
    body_style = ParagraphStyle(name="Body", fontName="Helvetica", fontSize=11, leading=15, spaceAfter=12)
    date_style = ParagraphStyle(name="Date", fontName="Helvetica", fontSize=11, leading=15, spaceAfter=24)

    story = [Paragraph(_esc(datetime.now().strftime("%B %d, %Y")), date_style)]
    for para in text.split("\n\n"):
        para = para.strip()
        if para:
            story.append(Paragraph(_esc(para).replace("\n", "<br/>"), body_style))
    if len(story) == 1:
        story.append(Spacer(1, 0))

    doc.build(story)


def get_or_create_cover_letter(profile: dict, job_logger) -> str:
    """
    Returns the path to this job's cover letter PDF, generating and caching
    it under CVS_DIR/<company>/Cover Letter.pdf (alongside the tailored
    resume) on first use so retries don't re-call Ollama.
    """
    company = profile.get("company_name")
    if not company:
        raise ValueError("profile['company_name'] must be set before generating a cover letter")

    output_path = _cover_letter_path(company)
    if os.path.exists(output_path):
        job_logger.info(f"Reusing existing cover letter: {output_path}")
        return output_path

    text = generate_cover_letter_text(profile, job_logger)
    save_cover_letter_pdf(text, output_path)
    logger.info(f"Saved cover letter to {output_path}")
    job_logger.info(f"Saved cover letter to {output_path}")
    return output_path
