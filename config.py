import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Ollama settings
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# Google Sheet settings
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "service_account.json")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Sheet1")

# Automation Behavior settings
AUTO_SUBMIT = os.getenv("AUTO_SUBMIT", "True").lower() in ("true", "1", "yes")
HEADLESS = os.getenv("HEADLESS", "False").lower() in ("true", "1", "yes")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
SCREENSHOT_BEFORE_SUBMIT = os.getenv("SCREENSHOT_BEFORE_SUBMIT", "True").lower() in ("true", "1", "yes")

# Base paths
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
RESUME_DIR = os.path.join(PROJECT_ROOT, "resume")
PROFILE_JSON_PATH = os.path.join(PROJECT_ROOT, "profile.json")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

# Ensure necessary directories exist
os.makedirs(RESUME_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
