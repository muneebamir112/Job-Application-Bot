import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Ollama settings
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "35"))
# Resume parsing and open-ended answers involve much longer prompts/responses
# than a quick dropdown/checkbox pick - give those a reasonable allowance.
OLLAMA_LONG_TIMEOUT = int(os.getenv("OLLAMA_LONG_TIMEOUT", "75"))

# Google Sheet settings
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "service_account.json")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Profile1")

# Automation Behavior settings
AUTO_SUBMIT = os.getenv("AUTO_SUBMIT", "True").lower() in ("true", "1", "yes")
HEADLESS = os.getenv("HEADLESS", "False").lower() in ("true", "1", "yes")
# Stealth mode routes the browser through patchright + real Chrome (same approach
# GlassD's scraper uses) instead of plain Playwright/Chromium, to avoid the bot
# fingerprints that trigger CAPTCHA/anti-bot challenges on some job sites.
STEALTH_MODE = os.getenv("STEALTH_MODE", "True").lower() in ("true", "1", "yes")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
# Screenshot is taken only at the moment a submission is actually confirmed
FILL_REQUIRED_ONLY = os.getenv("FILL_REQUIRED_ONLY", "False").lower() in ("true", "1", "yes")
# successful (see form_filler.fill_and_submit_form) - not before submitting,
# and never for a Failed/Human Attention/Dry Run outcome.
SCREENSHOT_ON_SUCCESS = os.getenv("SCREENSHOT_ON_SUCCESS", "True").lower() in ("true", "1", "yes")
# When True, detect_captcha_or_login_wall always returns (None, "") so the bot
# skips every captcha/login-wall gate and only fills the form and saves Q&A to Sheet3.
SKIP_CAPTCHA_CHECK = os.getenv("SKIP_CAPTCHA_CHECK", "False").lower() in ("true", "1", "yes")

# Answer engine for AI-answered form fields: "legacy" (OllamaJobSession) or "v2" (modules/answer_engine:
# fact sheet + job description in a cached prompt, checked answers, never-blank failure ladder).
# The model and host are always OLLAMA_MODEL / OLLAMA_HOST above.
def _env_bool(name, default):
    return os.getenv(name, str(default)).lower() in ("true", "1", "yes")

ANSWER_ENGINE = os.getenv("ANSWER_ENGINE", "legacy").lower()
ANSWER_THINK = _env_bool("ANSWER_THINK", False)                   # model thinking (slower, no measured quality gain)
ANSWER_NUM_CTX = int(os.getenv("ANSWER_NUM_CTX", "8192"))
ANSWER_TEMPERATURE = float(os.getenv("ANSWER_TEMPERATURE", "0.7"))
ANSWER_NUM_PREDICT = int(os.getenv("ANSWER_NUM_PREDICT", "300"))
ANSWER_KEEP_ALIVE = os.getenv("ANSWER_KEEP_ALIVE", "30m")
ANSWER_MAX_ATTEMPTS = int(os.getenv("ANSWER_MAX_ATTEMPTS", "2"))
ANSWER_FACTS_SOURCE = os.getenv("ANSWER_FACTS_SOURCE", "auto").lower()   # auto | json | pdf | profile
ANSWER_AUDIT = _env_bool("ANSWER_AUDIT", True)                    # write answers.jsonl + fact_sheet.txt in the job log folder

# When True, skip uploading resume file into the form
SKIP_RESUME_UPLOAD = os.getenv("SKIP_RESUME_UPLOAD", "False").lower() in ("true", "1", "yes")


# Base paths
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RESUME_DIR = os.path.join(PROJECT_ROOT, "resume")
PROFILE_JSON_PATH = os.path.join(PROJECT_ROOT, "profile.json")
# Fields the resume itself doesn't contain (LinkedIn/GitHub links, work
# authorization, salary expectations, etc.) but the form filler still needs -
# lives next to the resume file itself so it's obvious where to edit it.
EXTRA_INFO_JSON_PATH = os.path.join(RESUME_DIR, "extra_info.json")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

# Where resume-bot saves each company's tailored resume (must match CVS_DIR
# in resume-bot/ollama_generate.py) - used to upload the resume tailored to
# each specific job's company, instead of the single generic one in
# RESUME_DIR (which is only used to build the base profile/answers).
import os
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CVS_DIR = os.getenv("RESUMES_SAVE_PATH", os.path.join(BASE_DIR, "CVs"))
CVS_ALT_DIR = os.path.join(BASE_DIR, "CVs-old")

# Successful-application screenshots are saved to
# SCREENSHOTS_DIR/<Company Name>/<screenshot>.png - a dedicated Desktop
# folder rather than LOGS_DIR, since these are meant as user-facing proof of
# each submitted application, not internal debug logs.
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "Job Application Screenshots")

# Ensure necessary directories exist
os.makedirs(RESUME_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Where resume-bot saves the structured tailored resume (<company>_<profile>_ollama.json); the answer engine reads facts from it.
ANSWER_RESUME_DATA_DIR = os.getenv("ANSWER_RESUME_DATA_DIR", os.path.join(BASE_DIR, "resume-bot", "data"))
