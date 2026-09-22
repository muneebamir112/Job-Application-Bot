import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Ollama settings
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "35"))
# Resume parsing and open-ended answers involve much longer prompts/responses
# than a quick dropdown/checkbox pick - give those a reasonable allowance.
OLLAMA_LONG_TIMEOUT = int(os.getenv("OLLAMA_LONG_TIMEOUT", "75"))

# Google Sheet settings
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "service_account.json")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Sheet1")

# Automation Behavior settings
AUTO_SUBMIT = os.getenv("AUTO_SUBMIT", "True").lower() in ("true", "1", "yes")
HEADLESS = os.getenv("HEADLESS", "False").lower() in ("true", "1", "yes")
# Stealth mode routes the browser through patchright + real Chrome (same approach
# GlassD's scraper uses) instead of plain Playwright/Chromium, to avoid the bot
# fingerprints that trigger CAPTCHA/anti-bot challenges on some job sites.
STEALTH_MODE = os.getenv("STEALTH_MODE", "True").lower() in ("true", "1", "yes")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
# Screenshot is taken only at the moment a submission is actually confirmed
# successful (see form_filler.fill_and_submit_form) - not before submitting,
# and never for a Failed/Human Attention/Dry Run outcome.
SCREENSHOT_ON_SUCCESS = os.getenv("SCREENSHOT_ON_SUCCESS", "True").lower() in ("true", "1", "yes")

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
CVS_DIR = r"C:\Users\webNcodes\Desktop\CVs"

# Successful-application screenshots are saved to
# SCREENSHOTS_DIR/<Company Name>/<screenshot>.png - a dedicated Desktop
# folder rather than LOGS_DIR, since these are meant as user-facing proof of
# each submitted application, not internal debug logs.
SCREENSHOTS_DIR = r"C:\Users\webNcodes\Desktop\Job Application Screenshots"

# Ensure necessary directories exist
os.makedirs(RESUME_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
