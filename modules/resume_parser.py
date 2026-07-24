import os
import json
import pdfplumber
import docx
import config
from modules.ollama_client import query_ollama
from modules.logger import logger

PROFILE_TEMPLATE = {
    "full_name": "",
    "first_name": "",
    "last_name": "",
    "email": "",
    "phone": "",
    "location": "",
    "linkedin": "",
    "github": "",
    "portfolio": "",
    "current_title": "",
    "years_experience": "",
    "skills": [],
    "education": [],
    "work_experience": [],
    "work_authorization": "",
    "visa_sponsorship_needed": "",
    "salary_expectation": "",
    "notice_period": "",
    "willing_to_relocate": "",
    "resume_file_path": "",
    "resume_text": ""
}

# Fields that must be present after parsing. If a resume genuinely doesn't
# contain one, we null it explicitly rather than let it silently stay as an
# empty string (which the form filler would otherwise treat as "no value,
# fall back to Ollama").
REQUIRED_PROFILE_KEYS = [
    "full_name", "email", "phone", "linkedin", "github", "portfolio",
    "location", "years_experience", "skills", "education", "work_experience"
]

def apply_null_defaults_and_warn(profile: dict):
    """
    Ensures required keys have an explicit value or None (never a silent
    empty string/list), logging a warning for anything genuinely missing.
    """
    for key in REQUIRED_PROFILE_KEYS:
        value = profile.get(key)
        is_empty = value is None or value == "" or value == []
        if is_empty:
            profile[key] = None
            logger.warning(f"Profile field '{key}' was not found in the resume. Leaving as null.")

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extracts raw text from a PDF file."""
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text

def extract_text_from_docx(docx_path: str) -> str:
    """Extracts raw text from a DOCX file."""
    doc = docx.Document(docx_path)
    return "\n".join([para.text for para in doc.paragraphs])

def find_resume_file() -> str:
    """Searches config.RESUME_DIR for resume files."""
    if not os.path.exists(config.RESUME_DIR):
        os.makedirs(config.RESUME_DIR)
        return ""
    
    for f in os.listdir(config.RESUME_DIR):
        if f.lower().endswith(('.pdf', '.docx')):
            return os.path.join(config.RESUME_DIR, f)
    return ""

def parse_resume_to_json(resume_text: str) -> dict:
    """Uses Ollama to parse raw resume text into structured JSON."""
    system_prompt = (
        "You are an expert data parser. Your task is to extract information from the resume text "
        "and return it strictly formatted in JSON matching the requested schema. "
        "Do not hallucinate or guess any details. If a field is not found or not clear, leave it empty. "
        "Return ONLY the raw JSON object, without any markdown formatting or extra commentary."
    )
    
    prompt = f"""
    Analyze the following resume text and extract the details into the JSON format specified below:
    
    JSON Schema:
    {{
      "full_name": "",
      "email": "",
      "phone": "",
      "location": "City, Country (or City, State) if mentioned in the resume",
      "linkedin": "Full LinkedIn profile URL if present",
      "github": "Full GitHub profile URL if present",
      "portfolio": "Personal website/portfolio URL if present",
      "current_title": "",
      "years_experience": "",
      "skills": ["skill1", "skill2"],
      "education": [{{"degree":"","school":"","year":""}}],
      "work_experience": [{{"company":"","title":"","start":"","end":"","summary":""}}],
      "work_authorization": "Specify work authorization details if clear, otherwise empty",
      "visa_sponsorship_needed": "Yes/No or empty",
      "salary_expectation": "",
      "notice_period": "",
      "willing_to_relocate": "Yes/No or empty"
    }}

    Resume text:
    ---
    {resume_text}
    ---
    
    Output strictly in JSON format matching the schema:
    """
    
    response = query_ollama(prompt, system_prompt=system_prompt, timeout=config.OLLAMA_LONG_TIMEOUT)
    
    # Strip markdown block formatting if model includes it
    cleaned_response = response.strip()
    if cleaned_response.startswith("```json"):
        cleaned_response = cleaned_response[7:]
    if cleaned_response.startswith("```"):
        cleaned_response = cleaned_response[3:]
    if cleaned_response.endswith("```"):
        cleaned_response = cleaned_response[:-3]
    cleaned_response = cleaned_response.strip()

    try:
        parsed_data = json.loads(cleaned_response)
        return parsed_data
    except Exception as e:
        logger.error(f"Failed to parse JSON response from Ollama: {e}")
        logger.error(f"Raw Ollama response: {response}")
        # Return empty template as fallback
        return {}

def populate_name_fields(profile: dict):
    """Automatically splits full_name into first_name and last_name if missing."""
    full_name = profile.get("full_name", "").strip()
    if full_name:
        parts = full_name.split()
        if len(parts) > 1:
            profile["first_name"] = parts[0]
            profile["last_name"] = " ".join(parts[1:])
        elif len(parts) == 1:
            profile["first_name"] = parts[0]
            profile["last_name"] = ""

def get_or_create_profile() -> dict:
    """
    Retrieves the structured profile.json.
    Re-parses the resume if profile.json does not exist or if the resume file has been updated.
    """
    resume_path = find_resume_file()
    
    if not resume_path:
        # Check if profile.json already exists
        if os.path.exists(config.PROFILE_JSON_PATH):
            logger.info("No resume file found, but using existing profile.json.")
            with open(config.PROFILE_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        else:
            # Create a blank profile template for the user to fill
            logger.warning(f"No resume file (.pdf/.docx) found in '{config.RESUME_DIR}'. Creating blank profile.json template.")
            blank_profile = PROFILE_TEMPLATE.copy()
            blank_profile["resume_file_path"] = ""
            with open(config.PROFILE_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(blank_profile, f, indent=4)
            return blank_profile

    # Determine if parsing is needed
    reparse = False
    if not os.path.exists(config.PROFILE_JSON_PATH):
        reparse = True
    else:
        resume_mtime = os.path.getmtime(resume_path)
        profile_mtime = os.path.getmtime(config.PROFILE_JSON_PATH)
        if resume_mtime > profile_mtime:
            reparse = True
            logger.info("Resume file modification time is newer than profile.json. Re-parsing...")

    if reparse:
        logger.info(f"Parsing resume file: {resume_path}")
        if resume_path.lower().endswith(".pdf"):
            resume_text = extract_text_from_pdf(resume_path)
        else:
            resume_text = extract_text_from_docx(resume_path)
            
        parsed_profile = parse_resume_to_json(resume_text)

        # Merge with default template
        profile = PROFILE_TEMPLATE.copy()
        profile.update(parsed_profile)
        profile["resume_file_path"] = os.path.relpath(resume_path, config.PROJECT_ROOT)
        profile["resume_text"] = resume_text.strip()

        # Split names
        populate_name_fields(profile)

        # Never let a genuinely missing field silently fall back to Ollama later
        apply_null_defaults_and_warn(profile)

        # Write cache
        with open(config.PROFILE_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=4)
        logger.info(f"Successfully cached structured resume profile to {config.PROFILE_JSON_PATH}")
        return profile
    else:
        logger.info("Using cached profile.json")
        with open(config.PROFILE_JSON_PATH, "r", encoding="utf-8") as f:
            profile = json.load(f)
            # Ensure path is correct
            profile["resume_file_path"] = os.path.relpath(resume_path, config.PROJECT_ROOT)
            return profile

if __name__ == "__main__":
    # Test execution
    print(get_or_create_profile())
