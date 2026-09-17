import os
import re
import json
import difflib
import asyncio
from datetime import datetime
from playwright.async_api import Page, ElementHandle
import config
from modules.logger import logger
from modules.ollama_client import query_ollama
from modules.captcha_detector import detect_captcha_or_login_wall
from modules.text_utils import strip_markdown_formatting
import sys
_cl_bot_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "cover_letter_bot"))
if _cl_bot_path not in sys.path:
    sys.path.append(_cl_bot_path)
from cover_letter import get_or_create_cover_letter  # type: ignore

# ---------------------------------------------------------------------------
# Field classification
#
# Every detected form field is classified into exactly one of:
#   PROFILE_FIELD  - factual field, filled deterministically from profile.json.
#                    Ollama is never called for these.
#   CHOICE_FIELD   - select / radio / checkbox / autocomplete combobox. Answered
#                    by picking one of the actually rendered options, matched
#                    against profile.json when possible. Ollama (when used as a
#                    last resort) only ever picks among existing options, never
#                    free text.
#   OPEN_ENDED     - subjective / descriptive questions with no profile.json
#                    equivalent. The only classification allowed to produce a
#                    free-text Ollama answer.
#
# None of the selectors below are specific to any one ATS/site - they key off
# generic HTML/ARIA semantics (input types, role attributes, required/aria-
# invalid state) so the same code path runs on Greenhouse, Lever, Ashby,
# Workday, iCIMS, SmartRecruiters, or a fully custom career page.
# ---------------------------------------------------------------------------

CHOICE_FIELD_TYPES = {"select", "radio", "checkbox", "combobox"}

MATCH_THRESHOLD = 0.55
LISTBOX_WAIT_MS = 1500
SETTLE_TIMEOUT_MS = 3000
SETTLE_POLL_MS = 300
MAX_VALIDATION_RECOVERY_PASSES = 2

GENERIC_FIELD_SELECTOR = (
    "input:not([type='hidden']), textarea, select, "
    "[contenteditable='true'], [role='combobox'], [role='listbox'], "
    "[role='radiogroup'], [role='checkbox']"
)

PROFILE_FIELD_SYNONYMS = {
    "first_name": ["first name", "given name"],
    # middle_name: intentionally absent from open-ended matching — handled as a
    # special identity field below. The bot leaves it blank if not in profile.
    "middle_name": ["middle name", "middle initial"],
    "last_name": ["last name", "surname", "family name"],
    "full_name": ["full name", "your name", "candidate name", "applicant name", "legal name"],
    "email": ["email", "e-mail", "email address", "e-mail address"],
    "phone": ["phone", "mobile", "telephone", "phone number", "contact number"],
    "location": [
        "location", "city", "current city", "current location", "residence",
        "address", "where are you based", "based in"
    ],
    "state": ["state", "province", "region"],
    "country": ["country", "country/region", "country of residence"],
    "linkedin": ["linkedin", "linked in", "linkedin url", "linkedin profile"],
    "github": ["github", "git hub", "github url", "github profile"],
    "portfolio": ["portfolio", "website", "personal website", "blog", "portfolio url"],
    "current_title": ["current title", "headline", "job title", "current role", "current position"],
    "current_company": ["current company", "employer name"],
    "years_experience": ["years of experience", "experience level", "experience years", "years exp", "total experience"],
    "work_authorization": ["work authorization", "authorized to work", "legal authorization", "right to work", "employment authorization"],
    "visa_sponsorship_needed": ["visa", "sponsorship", "sponsor", "require visa", "need sponsorship", "require sponsorship"],
    "salary_expectation": ["salary", "compensation", "expected salary", "salary expectation", "desired salary"],
    "notice_period": ["notice period", "notice", "availability", "start date", "when can you start"],
    "willing_to_relocate": ["relocate", "relocation", "willing to relocate"],
    "remote_work": ["open to remote", "remote work preference", "willing to work remote", "work remotely", "prefer remote"],
    "golang_experience": ["golang", "go language", "experience in golang", "golang experience", "experience with golang", "professional development experience in golang"],
    "timezone": ["time zone", "timezone", "us time zone", "what time zone", "which time zone", "your time zone", "located in"],
    "skills": ["skills", "key skills", "technical skills", "list your skills"],
    # education_degree matches degree-level dropdowns/selects (e.g. "Bachelor's Degree")
    "education_degree": [
        "degree", "highest degree", "highest level of education", "education level",
        "degree type", "level of education", "degree earned", "degree level",
        "what is your highest",
    ],
    # education_school matches institution name text inputs
    "education_school": [
        "school", "university", "college", "institution", "school name",
        "where did you attend", "name of school",
    ],
    # education_year matches graduation year fields
    "education_year": [
        "graduation year", "year of graduation", "year graduated",
        "when did you graduate", "grad year",
    ],
    # education (full block) — used for open-text academic background summaries
    "education": ["education", "academic background", "qualification"],
    "referred_by": ["referred by", "who referred you", "referral name", "employee referral", "referrer", "referral"],
}


def normalize_text(text: str) -> str:
    """Lowercases, strips punctuation, and collapses whitespace for comparison."""
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def find_profile_synonym_match(label: str) -> str | None:
    """Returns the profile.json key whose synonym list matches the label, or None."""
    label_norm = normalize_text(label)
    if not label_norm:
        return None
    for key, keywords in PROFILE_FIELD_SYNONYMS.items():
        for kw in keywords:
            kw_norm = normalize_text(kw)
            if kw_norm and (kw_norm in label_norm or label_norm in kw_norm):
                return key
    return None


def classify_field(label: str, field_type: str, profile: dict) -> tuple[str, str | None]:
    """
    Classifies a field into PROFILE_FIELD, CHOICE_FIELD, or OPEN_ENDED.
    Returns (classification, matched_profile_key_or_None).
    """
    if field_type in CHOICE_FIELD_TYPES:
        return "CHOICE_FIELD", find_profile_synonym_match(label)

    matched_key = find_profile_synonym_match(label)
    if matched_key:
        return "PROFILE_FIELD", matched_key
    return "OPEN_ENDED", None


def get_profile_value(profile: dict, key: str | None) -> str | None:
    """Fetches a profile.json value as a display-ready string, or None if unset."""
    if not key:
        return None

    # --- middle_name: personal identity field, never inferred ---
    # If not set in profile, return None so the field is left blank with no
    # Ollama call. This prevents the bot from typing sentences like
    # "My middle name is not provided in the profile."
    if key == "middle_name":
        return profile.get("middle_name") or None

    if key == "current_company":
        work_experience = profile.get("work_experience")
        if isinstance(work_experience, list) and work_experience:
            company = work_experience[0].get("company") if isinstance(work_experience[0], dict) else None
            return str(company).strip() or None if company else None
        return None

    # --- education sub-keys: extract only the relevant part of the education
    # entry so that select/dropdown matching hits the right option cleanly.
    # e.g. education_degree returns "Bachelor's Degree" not
    # "Bachelor's Degree, Weber State University, 2014".
    if key == "education_degree":
        edu_list = profile.get("education")
        if isinstance(edu_list, list) and edu_list:
            first = edu_list[0]
            if isinstance(first, dict):
                return str(first.get("degree", "")).strip() or None
        return None

    if key == "education_school":
        edu_list = profile.get("education")
        if isinstance(edu_list, list) and edu_list:
            first = edu_list[0]
            if isinstance(first, dict):
                return str(first.get("school", "")).strip() or None
        return None

    if key == "education_year":
        edu_list = profile.get("education")
        if isinstance(edu_list, list) and edu_list:
            first = edu_list[0]
            if isinstance(first, dict):
                return str(first.get("year", "")).strip() or None
        return None

    if key == "state":
        return profile.get("location", "").split(',')[1].strip() if ',' in profile.get("location", "") else ""

    if key == "country":
        return resolve_country_from_location(profile.get("location") or "")

    value = profile.get(key)
    if value is None:
        return None

    if isinstance(value, list):
        if key == "education":
            # Full education block (for open-text academic background fields)
            parts = []
            for edu in value:
                if isinstance(edu, dict):
                    piece = ", ".join(
                        str(p) for p in [edu.get("degree"), edu.get("school"), edu.get("year")] if p
                    )
                    if piece:
                        parts.append(piece)
            return "; ".join(parts) or None
        joined = ", ".join(str(v) for v in value if v)
        return joined or None

    value_str = str(value).strip()
    return value_str or None


def interpret_yes_no(value: str | None) -> bool | None:
    """Interprets a profile value as a boolean checkbox state, or None if ambiguous."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("yes", "y", "true", "1"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    return None


# ---------------------------------------------------------------------------
# Canada work-authorization / residency questions - a common pair of Yes/No
# questions on Canadian job postings ("Are you legally authorized to work in
# Canada?" / "Do you currently reside in Canada?"). Handled as a special case
# rather than the generic profile-synonym + Ollama fallback: work-authorization
# is treated as a blanket "Yes" claim, while residency is derived from the
# candidate's actual location in profile.json.
# ---------------------------------------------------------------------------

US_STATE_ABBREVIATIONS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia",
    "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt",
    "va", "wa", "wv", "wi", "wy", "dc",
}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}
CANADA_PROVINCE_ABBREVIATIONS = {"ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt"}
CANADA_PROVINCE_NAMES = {
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "nova scotia", "northwest territories",
    "nunavut", "ontario", "prince edward island", "quebec", "saskatchewan", "yukon",
}


def resolve_country_from_location(location: str) -> str:
    """Best-effort country guess from a free-text location string. Returns 'US', 'CA', or 'OTHER'."""
    if not location:
        return "OTHER"
    norm = normalize_text(location)
    tokens = set(norm.split())

    if "canada" in norm or (tokens & CANADA_PROVINCE_ABBREVIATIONS) or any(name in norm for name in CANADA_PROVINCE_NAMES):
        return "CA"
    if ("usa" in norm or "united states" in norm or "america" in norm
            or (tokens & US_STATE_ABBREVIATIONS) or any(name in norm for name in US_STATE_NAMES)):
        return "US"
    return "OTHER"


def is_canada_work_authorization_question(label_norm: str) -> bool:
    return "canada" in label_norm and any(term in label_norm for term in ("authoriz", "legally", "eligible to work"))


def is_canada_residency_question(label_norm: str) -> bool:
    return "canada" in label_norm and any(term in label_norm for term in ("reside", "residing", "residency", "live in"))


# ---------------------------------------------------------------------------
# US EEO Race/Ethnicity self-identification question - standardized wording
# across nearly every US job platform. Default to declining to answer; only
# if no decline option exists, fall back to a best-effort region/country
# mapping from the candidate's location (never an AI guess for this one,
# since it's a sensitive voluntary disclosure).
# ---------------------------------------------------------------------------

HISPANIC_LATINO_COUNTRIES = {
    "mexico", "puerto rico", "cuba", "spain", "dominican republic", "guatemala",
    "honduras", "el salvador", "nicaragua", "costa rica", "panama", "belize",
    "argentina", "brazil", "chile", "colombia", "ecuador", "peru", "venezuela",
    "bolivia", "paraguay", "uruguay",
}
BLACK_AFRICAN_AMERICAN_COUNTRIES = {
    "nigeria", "kenya", "ghana", "ethiopia", "south africa", "senegal", "uganda",
    "tanzania", "zimbabwe", "cameroon", "ivory coast", "cote d ivoire", "mali",
    "somalia", "sudan", "congo", "jamaica", "haiti", "trinidad and tobago",
}
PACIFIC_ISLANDER_COUNTRIES = {
    "hawaii", "guam", "samoa", "fiji", "tonga", "palau", "micronesia", "marshall islands",
}
ASIAN_COUNTRIES = {
    "china", "india", "japan", "korea", "south korea", "north korea", "malaysia",
    "pakistan", "philippines", "thailand", "vietnam", "indonesia", "bangladesh",
    "sri lanka", "nepal", "cambodia", "laos", "myanmar", "singapore", "taiwan",
    "hong kong", "mongolia", "bhutan",
}
WHITE_COUNTRIES = {
    "united kingdom", "england", "scotland", "wales", "ireland", "france", "germany",
    "italy", "netherlands", "belgium", "switzerland", "austria", "sweden", "norway",
    "denmark", "finland", "poland", "portugal", "greece", "russia", "ukraine",
    "israel", "iran", "iraq", "egypt", "morocco", "tunisia", "algeria", "lebanon",
    "australia", "new zealand",
}

# Priority order matters: an origin can plausibly match more than one bucket's
# loose keyword set, so check the most specific/distinguishing groups first.
EEO_RACE_REGION_MAP = [
    ("Hispanic or Latino", HISPANIC_LATINO_COUNTRIES),
    ("Black or African American", BLACK_AFRICAN_AMERICAN_COUNTRIES),
    ("Native Hawaiian or Other Pacific Islander", PACIFIC_ISLANDER_COUNTRIES),
    ("Asian", ASIAN_COUNTRIES),
    ("White", WHITE_COUNTRIES),
]


def resolve_eeo_race_category(location: str) -> str | None:
    """Best-effort EEO race/ethnicity category guess from a free-text location string, or None if no match."""
    if not location:
        return None
    norm = normalize_text(location)
    for category, countries in EEO_RACE_REGION_MAP:
        if any(country in norm for country in countries):
            return category
    return None


def is_eeo_race_question(options_texts: list[str]) -> bool:
    combined = " ".join(normalize_text(t) for t in options_texts)
    return "hispanic or latino" in combined and "not hispanic" in combined


def best_matching_option(value: str, options_texts: list[str]) -> tuple[str | None, float]:
    """Fuzzy-matches a value against a list of rendered option texts. Returns (best_text, score)."""
    if not value or not options_texts:
        return None, 0.0

    value_norm = normalize_text(value)
    if not value_norm:
        return None, 0.0

    # Expand common country / region aliases to prevent short abbreviations
    # from incorrectly substring-matching other countries (e.g. 'US' in 'Australia')
    val_aliases = {value_norm}
    if value_norm in ("us", "usa", "united states", "america", "united states of america"):
        val_aliases.update({"us", "usa", "united states", "united states of america", "america", "1"})
    elif value_norm in ("ca", "can", "canada"):
        val_aliases.update({"ca", "canada"})

    best_text = None
    best_score = 0.0
    for opt in options_texts:
        opt_norm = normalize_text(opt)
        if not opt_norm:
            continue
        if value_norm == opt_norm or opt_norm in val_aliases:
            return opt, 1.0

        opt_tokens = set(opt_norm.split())

        # Exact alias phrase/token match (e.g. 'United States +1' matches 'US')
        if any(alias in opt_norm for alias in ("united states", "united states of america")) and ("us" in val_aliases):
            return opt, 0.98
        if val_aliases & opt_tokens:
            score = 0.95
        else:
            score = difflib.SequenceMatcher(None, value_norm, opt_norm).ratio()
            # Only do loose substring check if length > 3 or exact word boundary match,
            # which prevents short codes ('us', 'ca') from matching inside unrelated words ('australia').
            if len(value_norm) > 3 and (value_norm in opt_norm or opt_norm in value_norm):
                score = max(score, 0.85)
            elif re.search(rf"\b{re.escape(value_norm)}\b", opt_norm):
                score = max(score, 0.85)

        # Token overlap catches cases plain sequence-similarity misses, e.g.
        # "Haltom City, TX" vs a rendered "Haltom City, Texas, United States" -
        # most of the words genuinely match even though "TX" != "Texas".
        value_tokens = set(value_norm.split())
        if value_tokens:
            overlap_ratio = len(value_tokens & opt_tokens) / len(value_tokens)
            if overlap_ratio >= 0.66:
                score = max(score, 0.7 + 0.2 * overlap_ratio)
        if score > best_score:
            best_score = score
            best_text = opt
    return best_text, best_score


VISA_SPONSORSHIP_LABEL_KEYWORDS = ("visa", "sponsorship", "sponsor")
WORK_AUTHORIZATION_STATEMENT_KEYWORDS = ("authorized to work", "legally authorized")

# ---------------------------------------------------------------------------
# Permanent hardcoded answer rules (apply to ALL forms, ALL profiles)
#
# These fire before any profile.json lookup or Ollama call and cannot be
# overridden by any per-profile setting.
# ---------------------------------------------------------------------------

# Pronouns: always He/Him. Fallback to "Use name only" if He/Him not present.
PRONOUNS_PREFERRED = ["he/him", "he him"]
PRONOUNS_FALLBACK = ["use name only", "prefer not to say"]

# Gender: always Male
GENDER_LABEL_KEYWORDS = ("gender", "sex")
GENDER_ANSWER = "male"

# Citizenship: always U.S. Citizen
CITIZENSHIP_LABEL_KEYWORDS = ("citizen", "work status")
CITIZENSHIP_ANSWER = "u s citizen" # normalized form of U.S. Citizen

# Language: always English
LANGUAGE_LABEL_KEYWORDS = ("language", "languages spoken")
LANGUAGE_ANSWER = "english"

# Veteran Status: always "I am not a protected veteran"
VETERAN_LABEL_KEYWORDS = ("veteran", "protected veteran")
VETERAN_ANSWER_KEYWORDS = ("not a protected veteran", "not a veteran", "no, i am not", "i am not a")

# Disability Status: always "I don't have a disability"
DISABILITY_LABEL_KEYWORDS = ("disability", "disabilities")
DISABILITY_ANSWER_KEYWORDS = ("don't have", "do not have", "no, i don't", "no, i do not")

# Any label containing these keywords → always answer YES
ALWAYS_YES_LABEL_KEYWORDS = (
    "remote work experience",
    "remote experience",
    "do you have remote",
    "currently located in the united states",
    "currently located in united states",
    "located in the us",
    "authorized to work in the us",
    "authorized to work in the united states",
    "work authorization",
    "legally authorized to work",
    "3 consecutive years",
    "public trust clearance",
    "i agree",
    "agree to",
    "i consent",
    "consent to",
    "terms of service",
    "terms and conditions",
    "privacy policy",
    "i acknowledge",
    "i certify",
    "by checking this box",
)

# Any label containing these keywords → always answer NO
ALWAYS_NO_LABEL_KEYWORDS = (
    "require sponsorship",
    "need sponsorship",
    "will you require",
    "require visa",
    "require work visa",
    "visa sponsorship",
    "conflict of interest",
    "close personal relationships",
)


def resolve_visa_sponsorship_choice(label_norm: str, options_texts: list[str], profile: dict) -> str | None:
    """
    Visa/sponsorship questions are sometimes rendered as full statement
    options ("I am legally authorized to work in the USA." / "I require
    assistance immediately." / "I require assistance in the future.")
    instead of a plain Yes/No toggle. Fuzzy-matching a short profile value
    like "No" against those long, dissimilar sentences is unreliable (no
    shared words to anchor on), so whenever the candidate doesn't need
    sponsorship, always prefer whichever option explicitly states legal work
    authorization over the generic similarity matcher. Returns None to fall
    through to the generic matcher for genuine Yes/No-style options (where
    the normal matcher already works fine) or when sponsorship IS needed.
    """
    if not any(kw in label_norm for kw in VISA_SPONSORSHIP_LABEL_KEYWORDS):
        return None

    needs_sponsorship = interpret_yes_no(profile.get("visa_sponsorship_needed"))
    if needs_sponsorship is not False:
        return None

    for opt_text in options_texts:
        opt_norm = normalize_text(opt_text)
        if any(kw in opt_norm for kw in WORK_AUTHORIZATION_STATEMENT_KEYWORDS):
            return opt_text
    return None


def log_field_decision(job_logger, label: str, classification: str, source: str, value) -> None:
    """Structured per-field log line: label -> classification -> source -> final value."""
    display_value = value
    if isinstance(display_value, str) and len(display_value) > 120:
        display_value = display_value[:120] + "..."
    job_logger.info(f"[FIELD] label='{label}' classification={classification} source={source} value={display_value!r}")


def json_context_string(profile: dict) -> str:
    """Formats the profile details cleanly for Ollama context (excludes bulky/raw fields)."""
    filtered_profile = {k: v for k, v in profile.items() if k not in ("resume_file_path", "resume_text")}
    return json.dumps(filtered_profile, indent=2)


async def ask_ollama_open_ended(label: str, profile: dict, job_logger, classification: str) -> str:
    """The only call site allowed to generate a free-text Ollama answer for a form field."""
    assert classification == "OPEN_ENDED", "Ollama may only produce free text for OPEN_ENDED fields"

    profile_context = json_context_string(profile)
    resume_text = (profile.get("resume_text") or "")[:4000]
    job_title = profile.get("job_title") or "the role"
    company_name = profile.get("company_name") or "the company"
    profile_name = profile.get("name") or profile.get("full_name") or "the candidate"
    system_prompt = (
        "You are the candidate applying for this job. "
        "The Candidate Profile and Resume provided are YOUR personal background, YOUR experience, and YOUR identity. "
        "You MUST answer the question in the first person ('I', 'my', 'me'). "
        "NEVER refer to 'the candidate', 'the profile', 'the resume', or yourself as an AI. "
        "If a question asks for something not explicitly stated in your background, use your intelligence and professional judgment to deduce a reasonable, realistic answer as if you were this person. "
        "IMPORTANT: Only reply with EXACTLY 'N/A' if the question asks for a specific factual URL or account link (like a Twitter/GitHub URL) that is completely missing from the profile. "
        "For ALL other questions — including subjective, experience-based, preference, or opinion questions — you MUST write a proper first-person answer. NEVER reply N/A to those. "
        "Keep your answer concise, professional, and specific (2-4 sentences). "
        "This answer is typed directly into a plain-text form field, so write in plain prose only: "
        "no markdown, no **bold**, no headers, no bullet points, or asterisks. "
        f"You are applying for the '{job_title}' position at '{company_name}'. If the question or answer "
        "references the role or company, use those exact names. Never output placeholder text like [Company]."
    )
    prompt = f"""
Job title you are applying for: {job_title}
Company you are applying to: {company_name}

Your Profile context:
{profile_context}

Your Resume text:
{resume_text}

Question:
{label}

Answer the question professionally, concisely, and specifically, using only the facts above as YOUR own background.
If you reference the role or company, use the exact job title and company name given above -
never leave placeholder brackets like [Position Title] or [Company Name] in the answer.
Remember: You MUST answer in the first person ('I') and NEVER mention that you are an AI or reading from a profile.
Write plain prose only - act as {profile_name}, no markdown formatting of any kind:
"""
    job_logger.info(f"Open-ended field detected: '{label}'. Querying Ollama...")
    job_logger.info(f"--- OLLAMA PROMPT FOR '{label}' ---\nSystem: {system_prompt}\nUser: {prompt}\n----------------------------------")
    answer = query_ollama(prompt, system_prompt=system_prompt, timeout=config.OLLAMA_LONG_TIMEOUT)
    clean_answer = strip_markdown_formatting(answer)
    
    # Log all generated answers so the user can review and add them to the profile later
    try:
        with open(os.path.join(config.PROJECT_ROOT, "generated_answers.log"), "a", encoding="utf-8") as f:
            profile_name = profile.get("name") or profile.get("full_name") or "Unknown"
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Profile: {profile_name}\n")
            f.write(f"Question: {label}\n")
            f.write(f"Generated Answer: {clean_answer}\n")
            f.write("-" * 80 + "\n")
    except Exception as e:
        job_logger.error(f"Failed to log generated answer: {e}")

    # Collect for Sheet3 upload: {system_prompt, question, answer}
    if not hasattr(job_logger, "ollama_answers"):
        job_logger.ollama_answers = []
    job_logger.ollama_answers.append({
        "system_prompt": system_prompt,
        "question": label,
        "answer": clean_answer,
    })

    if clean_answer.strip().upper() == "N/A":
        clean_answer = "N/A"
    
    job_logger.info(f"--- OLLAMA RESPONSE FOR '{label}' ---\nRaw: {answer}\nCleaned: {clean_answer}\n------------------------------------")
    return clean_answer


async def ask_ollama_choice(label: str, options_texts: list[str], profile: dict, job_logger, classification: str) -> str:
    """The only call site allowed to ask Ollama to pick among existing rendered options (never free text)."""
    assert classification == "CHOICE_FIELD", "This helper only selects among existing rendered options"

    profile_context = json_context_string(profile)
    system_prompt = (
        "You are the candidate applying for this job. "
        "The Candidate Profile provided is YOUR personal background and YOUR identity. "
        "Output ONLY the exact text of the option that best matches the question/context based on your identity and background. "
        "NEVER refer to 'the candidate', 'the profile', or yourself as an AI. "
        "CRITICAL INSTRUCTION FOR SALARY: If a question asks whether a target salary range meets your requirements or expectations, and your profile's expected salary is LESS THAN or WITHIN that range, you MUST select 'Yes'. "
        "If a question asks for a preference or something not explicitly stated, use your professional judgment to deduce a reasonable answer as if you were this person. "
        "Do not include markdown or explanations. Output the exact option text only."
    )
    prompt = f"""
Your Profile:
{profile_context}

Question:
{label}

Options:
{options_texts}

Choose the single best matching option. Your response MUST be exactly one of the options from the list above:
"""
    job_logger.info(f"No profile match for choice field '{label}'. Asking Ollama to pick among existing options...")
    job_logger.info(f"--- OLLAMA PROMPT FOR '{label}' ---\nSystem: {system_prompt}\nUser: {prompt}\n----------------------------------")
    answer = query_ollama(prompt, system_prompt=system_prompt)
    job_logger.info(f"--- OLLAMA RESPONSE FOR '{label}' ---\n{answer}\n------------------------------------")
    return answer


async def ask_ollama_numeric(label: str, profile: dict, job_logger) -> str:
    """Queries Ollama for a pure numeric answer when an open-ended field expects a number/count/score."""
    profile_context = json_context_string(profile)
    system_prompt = (
        "You are the candidate applying for this job. "
        "The Candidate Profile provided is YOUR personal background. "
        "Answer the question with a single valid numeric value (digits only, e.g. 5 or 0). "
        "Do not output words, sentences, explanations, units, or punctuation. Output ONLY the number."
    )
    prompt = f"""
Your Profile:
{profile_context}

Question:
{label}

Answer with a single integer or number only:
"""
    job_logger.info(f"Numeric open-ended field detected: '{label}'. Querying Ollama for numeric answer...")
    job_logger.info(f"--- OLLAMA NUMERIC PROMPT FOR '{label}' ---\nSystem: {system_prompt}\nUser: {prompt}\n----------------------------------")
    answer = query_ollama(prompt, system_prompt=system_prompt, timeout=config.OLLAMA_TIMEOUT)
    clean_num = parse_numeric_value(answer, label=label)
    if not clean_num:
        clean_num = "0"
    job_logger.info(f"--- OLLAMA NUMERIC RESPONSE FOR '{label}' ---\nRaw: {answer}\nParsed: {clean_num}\n------------------------------------")
    return clean_num


# ---------------------------------------------------------------------------
# Frame-aware discovery & Chat Exclusion
#
# Playwright's CSS selector engine already pierces open shadow roots for
# query_selector_all()/locator() calls, so shadow DOM needs no special
# handling here. Iframes are a separate document tree though, so the form
# may live in the top-level page OR in an embedded iframe. Rather than
# hardcoding any site's iframe naming/URL pattern, we pick whichever frame
# currently has the most visible fillable fields - a generic structural
# signal that works the same way for every site.
# Chat widgets, virtual assistants, and support iframes are strictly ignored.
# ---------------------------------------------------------------------------

CHAT_IFRAME_PATTERNS = [
    "paradox", "intercom", "drift", "zendesk", "ada.support",
    "livechat", "tawk.to", "hubspot", "messenger", "chatbot",
    "jobchat", "virtualassistant", "userflow", "solvvy", "kustomer",
    "acs", "jibeapply"
]

def is_chat_frame(frame) -> bool:
    """Checks if a frame belongs to a third-party chat or virtual assistant widget."""
    try:
        url = (frame.url or "").lower()
        name = (frame.name or "").lower()
        for pat in CHAT_IFRAME_PATTERNS:
            if pat in url or pat in name:
                return True
    except Exception:
        pass
    return False

async def is_chat_or_support_element(elem) -> bool:
    """Detects if an element belongs to a chat widget, support bot, or virtual assistant."""
    try:
        is_chat = await elem.evaluate("""el => {
            const chatAncestor = el.closest(
                '[id*="chat" i], [class*="chat" i], [class*="paradox" i], [id*="paradox" i], ' +
                '[id*="intercom" i], [class*="intercom" i], [id*="drift" i], [class*="drift" i], ' +
                '[id*="zendesk" i], [class*="zendesk" i], [class*="messenger" i], [id*="messenger" i], ' +
                '[aria-label*="chat" i], [aria-label*="virtual assistant" i], [data-testid*="chat" i]'
            );
            if (chatAncestor) return true;

            const text = (
                (el.getAttribute('placeholder') || '') + ' ' +
                (el.getAttribute('aria-label') || '') + ' ' +
                (el.getAttribute('title') || '')
            ).toLowerCase();

            const chatPrompts = [
                'write a reply', 'type a message', 'ask a question', 'ask anything',
                'chat with', 'how can we help', 'send a message', 'talk to us',
                'type your message'
            ];
            return chatPrompts.some(p => text.includes(p));
        }""")
        return bool(is_chat)
    except Exception:
        return False

EXPIRED_JOB_PATTERNS = [
    "page you are looking for no longer exists",
    "job may be no longer available",
    "job is no longer available",
    "position is no longer available",
    "job posting has expired",
    "this position has been closed",
    "no longer accepting applications",
    "job not found",
    "404 - page not found",
    "the requisition has closed",
    "position has been filled",
    "this posting has expired",
    "posting is no longer active",
    "job expired",
    "we could not find the job"
]

async def detect_expired_or_missing_job(p_page) -> tuple[bool, str]:
    """Detects if the page is a 404, closed, or expired job notice."""
    try:
        title = (await p_page.title()).lower()
        if "404" in title or "not found" in title or "page not found" in title:
            return True, f"Job page title indicates 404/not found: '{title}'"
    except Exception:
        pass

    try:
        # Check main frame text and headings
        body_text = (await p_page.inner_text("body")).lower()
        for pat in EXPIRED_JOB_PATTERNS:
            if pat in body_text:
                return True, f"Job closure/expired notice detected: '{pat}'"
    except Exception:
        pass

    return False, ""

async def wait_for_embedded_ats(p_page, job_logger) -> None:
    """
    If the page contains external ATS embed indicators (Greenhouse, Lever, Ashby, etc.),
    wait for the container/iframe to load candidate fields before proceeding.
    """
    embed_selectors = [
        "script[src*='boards.greenhouse.io']",
        "#grnhse_app",
        "iframe[src*='greenhouse']",
        "iframe[src*='lever.co']",
        ".lever-jobs-container",
        "iframe[src*='ashbyhq']",
        "#ashby_embed",
        "iframe[src*='smartrecruiters']",
        "iframe[src*='jobvite']"
    ]
    has_embed = False
    for sel in embed_selectors:
        try:
            if await p_page.locator(sel).count() > 0:
                has_embed = True
                break
        except Exception:
            pass

    if has_embed:
        job_logger.info("Detected embedded ATS on host page. Waiting for ATS fields to mount...")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 8.0
        while loop.time() < deadline:
            frame = await select_active_frame(p_page)
            cnt = await count_visible_candidate_fields(frame)
            if cnt > 0:
                job_logger.info(f"Embedded ATS fields mounted ({cnt} fields detected).")
                break
            await asyncio.sleep(0.5)

async def count_visible_candidate_fields(frame) -> int:
    if is_chat_frame(frame):
        return 0
    try:
        elems = await frame.query_selector_all(GENERIC_FIELD_SELECTOR)
    except Exception:
        return 0
    count = 0
    for elem in elems:
        try:
            if await elem.is_visible():
                if await is_chat_or_support_element(elem):
                    continue
                count += 1
        except Exception:
            continue
    return count


async def select_active_frame(p_page):
    """Returns the Page's main frame or whichever embedded iframe has the most visible fields (ignoring chat iframes)."""
    best_frame = p_page.main_frame
    best_count = await count_visible_candidate_fields(best_frame)

    for frame in p_page.frames:
        if frame == p_page.main_frame:
            continue
        if is_chat_frame(frame):
            continue
        try:
            count = await count_visible_candidate_fields(frame)
        except Exception:
            count = 0
        if count > best_count:
            best_count = count
            best_frame = frame

    return best_frame


def _get_raw_playwright_page(page):
    p_page = page
    if hasattr(page, 'page'):
        p_page = page.page
    elif hasattr(page, 'get_playwright_page'):
        p_page = page.get_playwright_page()
    elif hasattr(page, '_page'):
        p_page = page._page
    return p_page


async def wait_for_fields_to_settle(frame, timeout_ms: int = SETTLE_TIMEOUT_MS, poll_ms: int = SETTLE_POLL_MS) -> None:
    """
    Bounded wait for dynamic rendering: polls the visible-field count until it
    stops changing (two consecutive identical reads) or the timeout elapses.
    If 0 fields are found, it will NOT exit early and will wait the full timeout,
    giving slow-loading forms or SPA navigations time to appear.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (timeout_ms / 1000)
    last_count = -1
    stable_hits = 0
    while loop.time() < deadline:
        count = await count_visible_candidate_fields(frame)
        if count == last_count and count > 0:
            stable_hits += 1
            if stable_hits >= 2:
                return
        else:
            stable_hits = 0
        last_count = count
        await asyncio.sleep(poll_ms / 1000)


# ---------------------------------------------------------------------------
# Autocomplete / combobox suggestion-list handling. One universal strategy
# for every type-ahead field regardless of platform: focus -> (optionally)
# type -> wait for the options list to render -> click the best match ->
# verify the committed value.
# ---------------------------------------------------------------------------

LISTBOX_OPTION_SELECTORS = [
    "[role='listbox'] [role='option']",
    "[role='option']",
    "ul[role='listbox'] li",
    ".select__menu .select__option",
    "[class*='menu'] [class*='option']",
    "[class*='suggestion']",
    "[class*='autocomplete'] li",
    "[data-automation-id*='option']",
    ".dropdown-item",
    # Google Places Autocomplete (used by Lever and others)
    ".pac-container .pac-item",
    # Generic typeahead/dropdown variants
    "ul[class*='dropdown'] li",
    "ul[class*='list'] li",
    "div[class*='option']",
    "div[class*='suggestion']",
]


async def find_visible_listbox_options(frame, wait_ms: int = LISTBOX_WAIT_MS) -> tuple[list[str], str | None, object]:
    """Polls briefly for a rendered suggestion/option list in both the frame and its parent page."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (wait_ms / 1000)
    
    contexts = [frame]
    if hasattr(frame, 'page') and frame.page != frame:
        contexts.append(frame.page)

    while loop.time() < deadline:
        for context in contexts:
            for selector in LISTBOX_OPTION_SELECTORS:
                try:
                    locator = context.locator(selector)
                    count = await locator.count()
                    if count == 0:
                        continue
                    texts = []
                    for i in range(min(count, 20)):
                        opt = locator.nth(i)
                        if await opt.is_visible():
                            text = (await opt.inner_text()).strip()
                            if text:
                                texts.append(text)
                    if texts:
                        return texts, selector, context
                except Exception:
                    continue
        await asyncio.sleep(0.2)
    return [], None, None


async def click_option_by_text(context, selector: str, text: str) -> bool:
    try:
        locator = context.locator(selector).filter(has_text=text).first
        if await locator.is_visible():
            await locator.click(timeout=5000)
            return True
    except Exception:
        pass
    return False


async def read_element_value(elem: ElementHandle) -> str:
    """Best-effort read of a field's current committed value, for post-selection verification."""
    try:
        tag = await elem.evaluate("el => el.tagName.toLowerCase()")
        if tag in ("input", "textarea", "select"):
            return (await elem.input_value()) or ""
        return ((await elem.evaluate("el => el.value || el.textContent || ''")) or "").strip()
    except Exception:
        return ""


async def handle_combobox_field(frame, elem: ElementHandle, label: str, profile: dict, job_logger, matched_key: str | None) -> bool:
    """
    Universal autocomplete/combobox strategy for any field detected as a
    combobox.
    """
    # Just like text fields, don't re-fill a combobox if it already has a value
    # during validation recovery or multi-step loops where the page didn't advance.
    existing_value = await read_element_value(elem)
    if existing_value and len(existing_value) > 1:
        return True

    value = get_profile_value(profile, matched_key) if matched_key else None
    is_location = matched_key == "location"

    try:
        await elem.scroll_into_view_if_needed(timeout=5000)
        await elem.click(timeout=5000)

        type_value = value
        if value and is_location:
            # The user requested to type only the city name (e.g. "Haltom City") 
            # instead of the full location string (e.g. "Haltom City, TX") to help 
            # the dropdown appear correctly.
            type_value = value.split(',')[0].strip()
        elif value and matched_key == "education_degree":
            # Type just the first prefix (e.g. "Bachelor") so it triggers dropdowns that might strictly expect "Bachelor's" with an apostrophe
            type_value = value.split("'")[0].split("’")[0].split()[0].strip()

        if type_value:
            # Clear any pre-existing content before typing
            await elem.evaluate("el => { if ('value' in el) el.value = ''; }")
            
            # The user requested to type characters one by one very slowly
            for char in type_value:
                # Type the single character
                await elem.press(char, delay=50)
                import asyncio
                # Keep a large gap (0.5 seconds) between each letter
                await asyncio.sleep(0.5)
            
            # Wait a bit extra after typing for the network request / dropdown to render
            await asyncio.sleep(1.0)

        option_texts, option_selector, target_context = await find_visible_listbox_options(frame)

        if option_texts:
            label_norm = normalize_text(label)
            best_text = None
            score = 0.0
            source = None

            # --- Permanent hardcoded rules (highest priority) ---
            if any(kw in label_norm for kw in CITIZENSHIP_LABEL_KEYWORDS):
                for opt in option_texts:
                    if normalize_text(opt) == CITIZENSHIP_ANSWER:
                        best_text = opt
                        score = 1.0
                        source = f"hardcoded-rule (citizenship: {CITIZENSHIP_ANSWER})"
                        break
            elif any(kw in label_norm for kw in LANGUAGE_LABEL_KEYWORDS):
                for opt in option_texts:
                    if normalize_text(opt) == LANGUAGE_ANSWER:
                        best_text = opt
                        score = 1.0
                        source = f"hardcoded-rule (language: {LANGUAGE_ANSWER})"
                        break
            elif any(kw in label_norm for kw in GENDER_LABEL_KEYWORDS):
                for opt in option_texts:
                    if normalize_text(opt) == GENDER_ANSWER:
                        best_text = opt
                        score = 1.0
                        source = f"hardcoded-rule (gender: {GENDER_ANSWER})"
                        break
            elif any(kw in label_norm for kw in VETERAN_LABEL_KEYWORDS):
                for ans_kw in VETERAN_ANSWER_KEYWORDS:
                    for opt in option_texts:
                        if ans_kw in normalize_text(opt):
                            best_text = opt
                            score = 1.0
                            source = "hardcoded-rule (veteran: decline)"
                            break
                    if best_text:
                        break
            elif any(kw in label_norm for kw in DISABILITY_LABEL_KEYWORDS):
                for ans_kw in DISABILITY_ANSWER_KEYWORDS:
                    for opt in option_texts:
                        if ans_kw in normalize_text(opt):
                            best_text = opt
                            score = 1.0
                            source = "hardcoded-rule (disability: decline)"
                            break
                    if best_text:
                        break

            if not best_text:
                forced_choice = resolve_visa_sponsorship_choice(label_norm, option_texts, profile)
                if forced_choice:
                    best_text = forced_choice
                    score = 1.0
                    source = "profile.json (visa sponsorship not needed -> legally authorized statement)"
                elif type_value:
                    best_text, score = best_matching_option(type_value, option_texts)
                    source = "dropdown match"
                    # Location: weak fuzzy match is still fine — the typed text
                    # filtered the list so the first suggestion is correct.
                if score < MATCH_THRESHOLD and is_location:
                    best_text = option_texts[0]
                    score = 1.0
                    source = "location-first-dropdown-result"
            else:
                choice = await ask_ollama_choice(label, option_texts, profile, job_logger, "CHOICE_FIELD")
                normalized_best, _ = best_matching_option(choice, option_texts)
                best_text = normalized_best or choice
                score = 1.0
                source = "ollama"

            if best_text and score >= MATCH_THRESHOLD:
                clicked = await click_option_by_text(target_context, option_selector, best_text)
                if clicked:
                    committed = await read_element_value(elem)
                    if committed and best_text.lower() not in committed.lower() and committed.lower() not in best_text.lower():
                        job_logger.warning(f"Combobox '{label}': committed value '{committed}' doesn't clearly match selected option '{best_text}'.")
                    log_field_decision(job_logger, label, "PROFILE_FIELD" if value else "CHOICE_FIELD", source, best_text)
                    return True

        # --- No dropdown appeared on initial load (or click didn't land) ---
        if value and is_location:
            # Step 4: Keyboard nudge & blind commit — some typeahead widgets (like Google
            # Places autocomplete) render their dropdowns in detached body elements or
            # ways that avoid our selectors. But they almost universally respond to
            # ArrowDown (to highlight the first suggestion) followed by Enter.
            try:
                await elem.press("ArrowDown")
                await asyncio.sleep(0.3)
                
                # Try to grab a visible option if it appeared this time
                option_texts2, option_selector2, target_context2 = await find_visible_listbox_options(frame, wait_ms=800)
                if option_texts2 and option_selector2:
                    clicked = await click_option_by_text(target_context2, option_selector2, option_texts2[0])
                    if clicked:
                        job_logger.info(f"Location '{label}': selected '{option_texts2[0]}' via ArrowDown nudge.")
                        log_field_decision(job_logger, label, "PROFILE_FIELD", "location-arrowdown-click", option_texts2[0])
                        return True
                
                # If still no visible option to click, blindly hit Enter to commit whatever
                # the ArrowDown highlighted (usually the top autocomplete suggestion).
                await elem.press("Enter")
                await asyncio.sleep(0.3)
                committed = await read_element_value(elem)
                # If the value changed significantly (e.g. "Haltom City" -> "Haltom City, TX"),
                # the autocomplete was successful.
                if committed and len(committed) > 3 and committed.lower() != type_value.lower():
                    job_logger.info(f"Location '{label}': blindly committed '{committed}' via ArrowDown+Enter.")
                    log_field_decision(job_logger, label, "PROFILE_FIELD", "location-keyboard-commit", committed)
                    return True
            except Exception:
                pass

            # Step 5: Dropdown never appeared and keyboard commit failed — field accepts plain text.
            committed = await read_element_value(elem)
            if not committed or committed.lower() == type_value.lower():
                await elem.evaluate("el => { if ('value' in el) el.value = ''; }")
                await elem.type(value, delay=30)
                await asyncio.sleep(0.2)
                await elem.press("Tab") # Help trigger blur validators

            job_logger.info(f"Location '{label}': no autocomplete dropdown appeared — keeping typed value '{value}' as plain text.")
            log_field_decision(job_logger, label, "PROFILE_FIELD", "profile.json (plain-text fallback)", value)
            return True

        if value:
            # Non-location combobox with no options rendered - plain text input
            log_field_decision(job_logger, label, "PROFILE_FIELD", "profile.json", value)
            return True

        job_logger.warning(f"Combobox '{label}' had no profile match and no options rendered on focus. Leaving unanswered.")
        log_field_decision(job_logger, label, "CHOICE_FIELD", "skipped-no-options", None)
        return False
    except Exception as e:
        job_logger.error(f"Failed to fill combobox field '{label}': {e}")
        return False


# ---------------------------------------------------------------------------
# Field label detection
# ---------------------------------------------------------------------------

async def _get_field_label_raw(frame, element: ElementHandle, is_group: bool = False) -> str:
    """
    Attempts to retrieve a human-readable label/placeholder/name for a given field
    element, prioritizing visible text over hidden attributes.
    If is_group is True, it specifically looks for a group-level question (like a 
    <legend> or an overarching question div) rather than the label of a single option.
    """
    try:
        # 1. Group-specific logic (for radio groups, yes/no buttons)
        if is_group:
            group_label = await element.evaluate("""el => {
                let fieldset = el.closest('fieldset');
                if (fieldset) {
                    let legend = fieldset.querySelector('legend');
                    if (legend && legend.innerText.trim()) return legend.innerText.trim();
                }
                let node = el;
                for (let depth = 0; depth < 5 && node; depth++) {
                    if (node.previousElementSibling) {
                        const text = node.previousElementSibling.innerText || node.previousElementSibling.textContent || '';
                        // Usually questions are longer than 5 chars, filters out tiny UI artifacts
                        if (text.trim() && text.trim().length > 5) return text.trim();
                    }
                    node = node.parentElement;
                }
                return '';
            }""")
            if group_label:
                return group_label

        # 2. Check explicit label associations
        elem_id = await element.get_attribute("id")
        if elem_id:
            label_elem = await frame.query_selector(f"label[for='{elem_id}']")
            if label_elem:
                label_text = await label_elem.inner_text()
                if label_text.strip():
                    return label_text.strip()

        labelledby = await element.get_attribute("aria-labelledby")
        if labelledby:
            for ref_id in labelledby.split():
                ref_elem = await frame.query_selector(f"[id='{ref_id}']")
                if ref_elem:
                    ref_text = await ref_elem.inner_text()
                    if ref_text.strip():
                        return ref_text.strip()

        parent_label_handle = await element.evaluate_handle("el => el.closest('label')")
        label_element = parent_label_handle.as_element()
        if label_element:
            label_text = await label_element.inner_text()
            if label_text.strip():
                return label_text.strip()

        # 4. Visible text heuristics (nearby <label> or previous text sibling)
        nearby_visible_text = await element.evaluate("""el => {
            // First look for a nearby <label> exactly 1 match
            let node = el;
            for (let depth = 0; depth < 8 && node; depth++) {
                node = node.parentElement;
                if (!node) break;
                const labels = node.querySelectorAll('label');
                if (labels.length === 1) {
                    const text = labels[0].innerText || labels[0].textContent || '';
                    if (text.trim()) return text.trim();
                } else if (labels.length > 1) {
                    break;
                }
            }
            
            // Look for a preceding heading/paragraph/div/span that reads like a question
            node = el;
            for (let depth = 0; depth < 8 && node; depth++) {
                if (node.previousElementSibling) {
                    const sib = node.previousElementSibling;
                    const text = sib.innerText || sib.textContent || '';
                    if (text.trim() && text.trim().length > 5) return text.trim();
                }
                node = node.parentElement;
            }

            // Look for a p/div/span with question-like text in the nearest ancestor container
            node = el;
            for (let depth = 0; depth < 8 && node; depth++) {
                node = node.parentElement;
                if (!node) break;
                // Look for paragraph, heading or div that looks like a question label
                const candidates = node.querySelectorAll('p, h1, h2, h3, h4, h5, h6, span, div');
                for (const c of candidates) {
                    if (c === el || c.contains(el)) continue;
                    if (c.querySelector('input, textarea, select, button')) continue;
                    const text = (c.innerText || c.textContent || '').trim();
                    if (text && text.length > 8 && text.length < 300) return text;
                }
            }
            
            // Finally, grab the first line of the parent container
            let parent = el.parentElement;
            if (parent) {
                const lines = parent.innerText.split('\\n').map(l => l.trim()).filter(l => l);
                if (lines.length > 0 && lines[0] !== (el.value || '')) {
                    return lines[0];
                }
            }
            return '';
        }""")
        
        if nearby_visible_text and nearby_visible_text.strip():
            return nearby_visible_text.strip()

        # 5. Fallback to attributes
        placeholder = await element.get_attribute("placeholder")
        if placeholder and placeholder.strip():
            return placeholder.strip()

        aria_label = await element.get_attribute("aria-label")
        if aria_label and aria_label.strip() and len(aria_label.strip()) > 3:
            return aria_label.strip()

        name_attr = await element.get_attribute("name")
        if name_attr and name_attr.strip() and 2 < len(name_attr.strip()) < 50:
            return name_attr.strip()

    except Exception as e:
        logger.debug(f"Error getting field label: {e}")

    return ""


async def get_field_label(frame, element: ElementHandle, is_group: bool = False) -> str:
    """Wrapper to retrieve and safely truncate labels to prevent massive DOM text dumps."""
    label = await _get_field_label_raw(frame, element, is_group)
    if label and len(label) > 200:
        return label[:197] + "..."
    return label or ""


async def is_combobox_element(element: ElementHandle) -> bool:
    """Detects text inputs that are actually type-ahead comboboxes."""
    try:
        role = (await element.get_attribute("role")) or ""
        aria_autocomplete = (await element.get_attribute("aria-autocomplete")) or ""
        aria_controls = (await element.get_attribute("aria-controls")) or ""
        has_list_attr = (await element.get_attribute("list")) or ""
        return bool(
            role.lower() == "combobox"
            or aria_autocomplete.lower() in ("list", "both")
            or aria_controls
            or has_list_attr
        )
    except Exception:
        return False


def parse_salary_value(val: str, label: str = "") -> str:
    """
    Parses salary strings into clean numeric format (e.g. '130k' -> '130000', '$130,000' -> '130000').
    Handles ranges ('120k - 140k') by selecting lower or upper bound based on label cues.
    """
    if not val:
        return ""
    val_str = str(val).strip()
    label_norm = normalize_text(label)

    # Check for range: '120k - 140k' or '120000 - 140000'
    parts = re.split(r"\s*(?:-|–|—|\bto\b)\s*", val_str)
    if len(parts) == 2:
        is_end = any(w in label_norm for w in ("end", "max", "maximum", "upper", "high", "to"))
        chosen = parts[1] if is_end else parts[0]
        return parse_salary_value(chosen, label)

    # Check for shorthand '130k' or '130.5k'
    k_match = re.search(r"(\d+(?:\.\d+)?)\s*[kK]\b", val_str)
    if k_match:
        try:
            num = float(k_match.group(1)) * 1000
            return str(int(num))
        except ValueError:
            pass

    # Clean non-digit characters except decimal dot
    digits = re.sub(r"[^\d.]", "", val_str)
    if digits:
        try:
            return str(int(float(digits)))
        except ValueError:
            return digits
    return val_str


def parse_numeric_value(val: str, label: str = "", elem_type: str = "", input_mode: str = "", matched_key: str | None = None) -> str:
    """
    Normalizes a value for numeric input fields or fields requiring clean numbers.
    - Salary fields: '130k' -> '130000'
    - Years of experience: '8+ Years Experience' -> '8'
    - Graduation year: '2014'
    - General numbers/counts: extracts leading digits
    """
    if not val:
        return ""
    val_str = str(val).strip()
    label_norm = normalize_text(label)

    if matched_key == "salary_expectation" or any(w in label_norm for w in ("salary", "compensation", "desired annual", "base salary")):
        return parse_salary_value(val_str, label)

    if matched_key == "years_experience" or any(w in label_norm for w in ("years of experience", "years exp", "how many years", "total years", "experience level")):
        match = re.search(r"(\d+(?:\.\d+)?)", val_str)
        if match:
            return match.group(1)

    if matched_key == "education_year" or any(w in label_norm for w in ("graduation year", "year of graduation", "grad year")):
        match = re.search(r"\b(19\d\d|20\d\d)\b", val_str)
        if match:
            return match.group(1)

    # If element type is strictly number or inputmode is numeric, ensure only valid digits
    if elem_type == "number" or input_mode in ("numeric", "decimal") or any(w in label_norm for w in ("numbers only", "digits only", "numeric only", "no commas")):
        match = re.search(r"(\d+(?:\.\d+)?)", val_str)
        if match:
            return match.group(1)

    return val_str


def is_numeric_field(elem_type: str, input_mode: str, pattern: str, label: str, placeholder: str = "") -> bool:
    """Determines whether a field strictly expects a number."""
    if elem_type == "number":
        return True
    if input_mode in ("numeric", "decimal"):
        return True

    label_norm = normalize_text(label)
    placeholder_norm = normalize_text(placeholder)
    combined = f"{label_norm} {placeholder_norm}"

    numeric_cues = (
        "no commas",
        "format with no commas",
        "numbers only",
        "digits only",
        "numeric only",
        "integer only",
        "whole numbers only",
        "how many years",
        "years of experience",
        "desired annual base salary",
        "salary range",
        "desired salary",
        "annual base salary",
        "gpa",
        "notice period in days",
        "notice period in weeks",
        "notice period (days)",
        "notice period (weeks)",
    )
    if any(cue in combined for cue in numeric_cues):
        return True

    if pattern and re.search(r"^\^?\[?0-9\d\]?\+?\$?$", pattern.strip()):
        return True

    return False


async def set_field_value(elem: ElementHandle, value: str) -> None:
    """Sets a value on a native input/textarea/select, or types into a contenteditable element."""
    is_editable = False
    try:
        is_editable = await elem.evaluate("el => el.isContentEditable === true")
    except Exception:
        pass

    try:
        input_type = (await elem.get_attribute("type") or "").lower()
        if input_type == "number":
            # Strip any non-digit/decimal characters to prevent Playwright Malformed input error
            match = re.search(r"(\d+(?:\.\d+)?)", str(value))
            if match:
                value = match.group(1)
            else:
                value = ""
    except Exception:
        pass

    try:
        await elem.scroll_into_view_if_needed(timeout=2000)
        await elem.click(timeout=3000)
    except Exception:
        try:
            await elem.click(force=True, timeout=2000)
        except Exception:
            pass

    try:
        await elem.press("Control+a", timeout=1000)
        await elem.press("Backspace", timeout=1000)
    except Exception:
        pass

    if is_editable:
        await elem.type(value, delay=20)
    else:
        await elem.fill(value)


# ---------------------------------------------------------------------------
# Field fillers
# ---------------------------------------------------------------------------

async def fill_text_field(frame, elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Fills a text input, textarea, or contenteditable element."""
    if not label or not label.strip():
        return False

    # The validation-recovery loop re-scans the whole form; skip fields that
    # already have a value rather than re-filling (and, for OPEN_ENDED fields,
    # re-querying Ollama) all over again on every retry pass.
    # However, do NOT skip if the field is marked aria-invalid (post-submit validation error).
    existing_value = await read_element_value(elem)
    if existing_value:
        try:
            is_invalid = await elem.get_attribute("aria-invalid")
            if is_invalid and is_invalid.lower() == "true":
                pass  # Field has a value but is marked invalid; fall through to re-fill
            else:
                return True
        except Exception:
            return True

    field_type = "combobox" if await is_combobox_element(elem) else "text"
    classification, matched_key = classify_field(label, field_type, profile)

    if classification == "CHOICE_FIELD":
        return await handle_combobox_field(frame, elem, label, profile, job_logger, matched_key)

    label_norm = normalize_text(label)

    # --- Permanent hardcoded rules for text fields (run before profile matching) ---
    if any(kw in label_norm for kw in ALWAYS_YES_LABEL_KEYWORDS):
        try:
            await elem.scroll_into_view_if_needed()
            await set_field_value(elem, "Yes")
            log_field_decision(job_logger, label, classification, "hardcoded-rule (always yes text)", "Yes")
            return True
        except Exception as e:
            job_logger.error(f"Failed to fill text field '{label}': {e}")
            return False

    if any(kw in label_norm for kw in ALWAYS_NO_LABEL_KEYWORDS):
        try:
            await elem.scroll_into_view_if_needed()
            await set_field_value(elem, "No")
            log_field_decision(job_logger, label, classification, "hardcoded-rule (always no text)", "No")
            return True
        except Exception as e:
            job_logger.error(f"Failed to fill text field '{label}': {e}")
            return False

    if matched_key == "location":
        return await handle_combobox_field(frame, elem, label, profile, job_logger, matched_key)

    # Inspect element attributes to detect if numeric input is required
    elem_type = ""
    input_mode = ""
    pattern = ""
    placeholder = ""
    try:
        elem_info = await elem.evaluate(
            """el => ({
                tagName: (el.tagName || '').toLowerCase(),
                type: (el.getAttribute('type') || '').toLowerCase(),
                inputMode: (el.getAttribute('inputmode') || '').toLowerCase(),
                pattern: el.getAttribute('pattern') || '',
                placeholder: el.getAttribute('placeholder') || ''
            })"""
        )
        elem_type = elem_info.get("type", "")
        input_mode = elem_info.get("inputMode", "")
        pattern = elem_info.get("pattern", "")
        placeholder = elem_info.get("placeholder", "")
    except Exception:
        pass

    numeric_required = is_numeric_field(elem_type, input_mode, pattern, label, placeholder)

    if classification == "PROFILE_FIELD":
        value = get_profile_value(profile, matched_key)
        if not value:
            # middle_name specifically: expected to be absent for most candidates.
            # Log at INFO, not WARNING, and leave the field blank — never Ollama.
            if matched_key == "middle_name":
                job_logger.info(f"No middle name in profile — leaving '{label}' blank.")
            else:
                job_logger.warning(f"Profile field '{matched_key}' has no value for '{label}'. Skipping (Ollama not used for profile fields).")
            log_field_decision(job_logger, label, classification, "skipped-empty-profile-value", None)
            return False

        # If field is numeric or key is numeric-related, normalize value
        if numeric_required or matched_key in ("salary_expectation", "years_experience", "education_year"):
            formatted_value = parse_numeric_value(value, label=label, elem_type=elem_type, input_mode=input_mode, matched_key=matched_key)
            if formatted_value:
                job_logger.info(f"Normalized numeric profile field '{label}': '{value}' -> '{formatted_value}'")
                value = formatted_value

        try:
            await elem.scroll_into_view_if_needed()
            await set_field_value(elem, value)
            log_field_decision(job_logger, label, classification, "profile.json", value)
            return True
        except Exception as e:
            job_logger.error(f"Failed to fill text field '{label}': {e}")
            return False

    # --- Permanent hardcoded rules for text fields ---
    label_norm = normalize_text(label)
    if "pronoun" in label_norm:
        val = "He/Him"
        try:
            await elem.scroll_into_view_if_needed()
            await set_field_value(elem, val)
            log_field_decision(job_logger, label, classification, "hardcoded-rule (pronouns: He/Him text)", val)
            return True
        except Exception as e:
            job_logger.error(f"Failed to fill text field '{label}': {e}")
            return False

    # OPEN_ENDED - the only path allowed to reach Ollama for a free-text or numeric answer
    try:
        if numeric_required:
            val = await ask_ollama_numeric(label, profile, job_logger)
        else:
            val = await ask_ollama_open_ended(label, profile, job_logger, classification)
        if val == "N/A":
            # For URL/link fields that are required, write "N/A" literally so field isn't empty.
            # For all other fields, leave blank (return False) so validation can catch it.
            label_lower = label.lower()
            is_url_field = any(kw in label_lower for kw in ("url", "link", "http", "website", "portfolio", "github", "linkedin", "credential"))
            if is_url_field:
                job_logger.info(f"Ollama returned N/A for URL field '{label}'. Writing 'N/A' to satisfy required field.")
                try:
                    await elem.scroll_into_view_if_needed()
                    await set_field_value(elem, "N/A")
                    log_field_decision(job_logger, label, classification, "hardcoded-na (no url in profile)", "N/A")
                    return True
                except Exception as e:
                    job_logger.error(f"Failed to write N/A into URL field '{label}': {e}")
                    return False
            job_logger.info(f"Ollama returned N/A for '{label}', leaving field blank.")
            log_field_decision(job_logger, label, classification, "skipped-unanswered", "N/A")
            return False  # Return False so required-field checks can still flag this as empty
    except Exception as e:
        job_logger.error(f"Ollama failed to answer open-ended question '{label}': {e}")
        return False

    if numeric_required:
        val = parse_numeric_value(val, label=label, elem_type=elem_type, input_mode=input_mode)

    try:
        await elem.scroll_into_view_if_needed()
        await set_field_value(elem, val)
        log_field_decision(job_logger, label, classification, "ollama (numeric)" if numeric_required else "ollama", val)
        return True
    except Exception as e:
        job_logger.error(f"Failed to fill text field '{label}': {e}")
        return False


async def fill_select_field(elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Fills a native <select> dropdown by choosing an existing option."""
    if not label or not label.strip():
        return False
    
    existing_value = await read_element_value(elem)
    if existing_value:
        return True

    try:
        await elem.scroll_into_view_if_needed()
        options_data = await elem.evaluate("el => Array.from(el.options).map(o => ({text: o.text, value: o.value}))")
        options_texts = [opt["text"].strip() for opt in options_data if opt["text"].strip()]

        if not options_texts:
            return False

        classification, matched_key = classify_field(label, "select", profile)
        profile_value = get_profile_value(profile, matched_key)
        label_norm = normalize_text(label)

        selected_option_text = None
        source = None

        # --- Permanent hardcoded rules (highest priority) ---
        if any(kw in label_norm for kw in CITIZENSHIP_LABEL_KEYWORDS):
            for opt in options_texts:
                if normalize_text(opt) == CITIZENSHIP_ANSWER:
                    selected_option_text = opt
                    source = f"hardcoded-rule (citizenship: {CITIZENSHIP_ANSWER})"
                    break
        elif any(kw in label_norm for kw in LANGUAGE_LABEL_KEYWORDS):
            for opt in options_texts:
                if normalize_text(opt) == LANGUAGE_ANSWER:
                    selected_option_text = opt
                    source = f"hardcoded-rule (language: {LANGUAGE_ANSWER})"
                    break
        elif any(kw in label_norm for kw in GENDER_LABEL_KEYWORDS):
            for opt in options_texts:
                if normalize_text(opt) == GENDER_ANSWER:
                    selected_option_text = opt
                    source = f"hardcoded-rule (gender: {GENDER_ANSWER})"
                    break
        elif any(kw in label_norm for kw in VETERAN_LABEL_KEYWORDS):
            for ans_kw in VETERAN_ANSWER_KEYWORDS:
                for opt in options_texts:
                    if ans_kw in normalize_text(opt):
                        selected_option_text = opt
                        source = "hardcoded-rule (veteran: decline)"
                        break
                if selected_option_text:
                    break
        elif any(kw in label_norm for kw in DISABILITY_LABEL_KEYWORDS):
            for ans_kw in DISABILITY_ANSWER_KEYWORDS:
                for opt in options_texts:
                    if ans_kw in normalize_text(opt):
                        selected_option_text = opt
                        source = "hardcoded-rule (disability: decline)"
                        break
                if selected_option_text:
                    break
        elif "pronoun" in label_norm:
            # Try He/Him first, then fallback to "Use name only"
            for preferred in PRONOUNS_PREFERRED:
                for opt in options_texts:
                    if normalize_text(opt) in [normalize_text(preferred), preferred.replace("/", " ")]:
                        selected_option_text = opt
                        source = "hardcoded-rule (pronouns: He/Him)"
                        break
                if selected_option_text:
                    break
            if not selected_option_text:
                for fallback in PRONOUNS_FALLBACK:
                    for opt in options_texts:
                        if normalize_text(fallback) in normalize_text(opt):
                            selected_option_text = opt
                            source = "hardcoded-rule (pronouns: Use name only fallback)"
                            break
                    if selected_option_text:
                        break
        elif any(kw in label_norm for kw in ALWAYS_YES_LABEL_KEYWORDS):
            for opt in options_texts:
                if normalize_text(opt) in ("yes", "y", "true") or normalize_text(opt).startswith("yes"):
                    selected_option_text = opt
                    source = "hardcoded-rule (always yes)"
                    break
        elif any(kw in label_norm for kw in ALWAYS_NO_LABEL_KEYWORDS):
            for opt in options_texts:
                if normalize_text(opt) in ("no", "n", "false") or normalize_text(opt).startswith("no"):
                    selected_option_text = opt
                    source = "hardcoded-rule (always no)"
                    break

        if not selected_option_text:
            forced_choice = resolve_visa_sponsorship_choice(label_norm, options_texts, profile)
            if forced_choice:
                selected_option_text = forced_choice
                source = "profile.json (visa sponsorship not needed -> legally authorized statement)"
            elif profile_value:
                best_text, score = best_matching_option(profile_value, options_texts)
                if best_text and score >= MATCH_THRESHOLD:
                    selected_option_text = best_text
                    source = "dropdown match"

        if not selected_option_text:
            selected_option_text = await ask_ollama_choice(label, options_texts, profile, job_logger, classification)
            source = "ollama"

        matching_value = None
        final_text = selected_option_text
        for opt in options_data:
            if opt["text"].strip().lower() == selected_option_text.strip().lower() or selected_option_text.strip().lower() in opt["text"].strip().lower():
                matching_value = opt["value"]
                final_text = opt["text"]
                break

        if matching_value is None:
            matching_value = options_data[0]["value"]
            final_text = options_data[0]["text"]
            source = "fallback-first-option"

        await elem.select_option(value=matching_value)
        log_field_decision(job_logger, label, classification, source, final_text)
        return True
    except Exception as e:
        job_logger.error(f"Failed to fill select field '{label}': {e}")
        return False


async def _resolve_choice_and_click(options: list[tuple], label: str, profile: dict, job_logger, classification: str) -> tuple[bool, str, str]:
    """Shared matching logic for native radio groups and ARIA [role=radio] groups."""
    options_texts = [text for _, text in options if text]
    if not options_texts:
        return False, "", ""

    label_norm = normalize_text(label)

    # --- Permanent hardcoded rules (highest priority) ---
    if any(kw in label_norm for kw in CITIZENSHIP_LABEL_KEYWORDS):
        for elem, opt_text in options:
            if normalize_text(opt_text) == CITIZENSHIP_ANSWER:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, f"hardcoded-rule (citizenship: {CITIZENSHIP_ANSWER})", opt_text

    if any(kw in label_norm for kw in LANGUAGE_LABEL_KEYWORDS):
        for elem, opt_text in options:
            if normalize_text(opt_text) == LANGUAGE_ANSWER:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, f"hardcoded-rule (language: {LANGUAGE_ANSWER})", opt_text

    if any(kw in label_norm for kw in GENDER_LABEL_KEYWORDS):
        for elem, opt_text in options:
            if normalize_text(opt_text) == GENDER_ANSWER:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, f"hardcoded-rule (gender: {GENDER_ANSWER})", opt_text

    if any(kw in label_norm for kw in VETERAN_LABEL_KEYWORDS):
        for ans_kw in VETERAN_ANSWER_KEYWORDS:
            for elem, opt_text in options:
                if ans_kw in normalize_text(opt_text):
                    await elem.scroll_into_view_if_needed()
                    await elem.click()
                    return True, "hardcoded-rule (veteran: decline)", opt_text

    if any(kw in label_norm for kw in DISABILITY_LABEL_KEYWORDS):
        for ans_kw in DISABILITY_ANSWER_KEYWORDS:
            for elem, opt_text in options:
                if ans_kw in normalize_text(opt_text):
                    await elem.scroll_into_view_if_needed()
                    await elem.click()
                    return True, "hardcoded-rule (disability: decline)", opt_text

    if any(kw in label_norm for kw in ALWAYS_YES_LABEL_KEYWORDS):
        # Click whichever option says "yes" (case-insensitive)
        for elem, opt_text in options:
            if normalize_text(opt_text) in ("yes", "y", "true"):
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "hardcoded-rule (always yes)", opt_text
        # Fallback: click first option whose text starts with "yes"
        for elem, opt_text in options:
            if normalize_text(opt_text).startswith("yes"):
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "hardcoded-rule (always yes - prefix match)", opt_text

    if any(kw in label_norm for kw in ALWAYS_NO_LABEL_KEYWORDS):
        for elem, opt_text in options:
            if normalize_text(opt_text) in ("no", "n", "false"):
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "hardcoded-rule (always no)", opt_text
        for elem, opt_text in options:
            if normalize_text(opt_text).startswith("no"):
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "hardcoded-rule (always no - prefix match)", opt_text

    # Pronouns radio group: prefer He/Him, fallback to "Use name only"
    if "pronoun" in label_norm:
        for preferred in PRONOUNS_PREFERRED:
            for elem, opt_text in options:
                if normalize_text(opt_text) in [normalize_text(preferred), preferred.replace("/", " ")]:
                    await elem.scroll_into_view_if_needed()
                    await elem.click()
                    return True, "hardcoded-rule (pronouns: He/Him)", opt_text
        for fallback in PRONOUNS_FALLBACK:
            for elem, opt_text in options:
                if normalize_text(fallback) in normalize_text(opt_text):
                    await elem.scroll_into_view_if_needed()
                    await elem.click()
                    return True, "hardcoded-rule (pronouns: Use name only fallback)", opt_text

    if is_eeo_race_question(options_texts):
        # US EEO race/ethnicity self-identification - default to declining;
        # only fall back to a location-based region guess if no decline
        # option exists on this particular form. Never an AI guess.
        for elem, opt_text in options:
            opt_norm = normalize_text(opt_text)
            if "decline" in opt_norm and "self identify" in opt_norm:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "profile.json (race/ethnicity always declined)", opt_text

        category = resolve_eeo_race_category(profile.get("location") or "")
        if category:
            best_text, score = best_matching_option(category, options_texts)
            if best_text and score >= MATCH_THRESHOLD:
                for elem, opt_text in options:
                    if opt_text == best_text:
                        await elem.scroll_into_view_if_needed()
                        await elem.click()
                        return True, f"profile.json (location-based race category: {category})", opt_text

    forced_choice = resolve_visa_sponsorship_choice(label_norm, options_texts, profile)
    if forced_choice:
        for elem, opt_text in options:
            if opt_text == forced_choice:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "profile.json (visa sponsorship not needed -> legally authorized statement)", opt_text

    matched_key = find_profile_synonym_match(label)
    profile_value = get_profile_value(profile, matched_key)

    selected_option_text = None
    source = None
    if profile_value:
        best_text, score = best_matching_option(profile_value, options_texts)
        if best_text and score >= MATCH_THRESHOLD:
            selected_option_text = best_text
            source = "dropdown match"

    if not selected_option_text:
        selected_option_text = await ask_ollama_choice(label, options_texts, profile, job_logger, classification)
        source = "ollama"

    for elem, opt_text in options:
        if opt_text and (opt_text.lower() == selected_option_text.lower() or selected_option_text.lower() in opt_text.lower()):
            await elem.scroll_into_view_if_needed()
            await elem.click()
            return True, source, opt_text

    await options[0][0].click()
    return True, "fallback-first-option", options[0][1]


async def fill_radio_group(frame, name_attr: str, label: str, profile: dict, job_logger) -> bool:
    """Fills a group of native radio buttons with the same name attribute."""
    try:
        radios = await frame.query_selector_all(f"input[type='radio'][name='{name_attr}']")
        radio_options = []
        for r in radios:
            r_id = await r.get_attribute("id")
            r_label = ""
            if r_id:
                l_elem = await frame.query_selector(f"label[for='{r_id}']")
                if l_elem:
                    r_label = await l_elem.inner_text()
            if not r_label.strip():
                r_label = await r.evaluate("el => el.parentElement.innerText")
            radio_options.append((r, r_label.strip()))

        # Some ATS forms pre-check a default option (e.g. "I require assistance
        # immediately." on a visa-sponsorship question) before the bot ever
        # touches the field. That default must be overridden, not mistaken for
        # an answer the bot already gave - so this forced-choice check runs
        # before the generic "already checked, skip" logic below.
        options_texts = [text for _, text in radio_options if text]
        forced_choice = resolve_visa_sponsorship_choice(normalize_text(label), options_texts, profile)
        if forced_choice:
            for r, opt_text in radio_options:
                if opt_text == forced_choice:
                    if await r.is_checked():
                        return True
                    await r.scroll_into_view_if_needed()
                    await r.click()
                    log_field_decision(job_logger, label, "CHOICE_FIELD",
                        "profile.json (visa sponsorship not needed -> legally authorized statement, overriding page default)", forced_choice)
                    return True

        # Radios themselves are idempotent to re-click (clicking an already-
        # selected one is a no-op), but skip anyway to avoid non-deterministic
        # re-answering (a fresh Ollama call could pick a different option) on
        # validation-recovery re-passes.
        for r, _ in radio_options:
            if await r.is_checked():
                return True

        classification, _ = classify_field(label, "radio", profile)
        success, source, final_text = await _resolve_choice_and_click(radio_options, label, profile, job_logger, classification)
        if success:
            log_field_decision(job_logger, label, classification, source, final_text)
        return success
    except Exception as e:
        job_logger.error(f"Failed to fill radio group '{label}': {e}")
        return False


async def fill_aria_radio_group(frame, group_elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Fills an ARIA [role=radiogroup] made of [role=radio] children (custom-widget radios)."""
    try:
        radios = await group_elem.query_selector_all("[role='radio']")
        radio_options = []
        for r in radios:
            r_label = (await r.get_attribute("aria-label")) or ""
            if not r_label.strip():
                r_label = (await r.inner_text()).strip()
            radio_options.append((r, r_label.strip()))

        # Overrides a page's pre-checked default (e.g. "I require assistance
        # immediately.") before the generic "already answered, skip" check
        # below mistakes it for an answer the bot already gave.
        options_texts = [text for _, text in radio_options if text]
        forced_choice = resolve_visa_sponsorship_choice(normalize_text(label), options_texts, profile)
        if forced_choice:
            for r, opt_text in radio_options:
                if opt_text == forced_choice:
                    if (await r.get_attribute("aria-checked")) == "true":
                        return True
                    await r.scroll_into_view_if_needed()
                    await r.click()
                    log_field_decision(job_logger, label, "CHOICE_FIELD",
                        "profile.json (visa sponsorship not needed -> legally authorized statement, overriding page default)", forced_choice)
                    return True

        for r, _ in radio_options:
            if (await r.get_attribute("aria-checked")) == "true":
                return True

        classification, _ = classify_field(label, "radio", profile)
        success, source, final_text = await _resolve_choice_and_click(radio_options, label, profile, job_logger, classification)
        if success:
            log_field_decision(job_logger, label, classification, source, final_text)
        return success
    except Exception as e:
        job_logger.error(f"Failed to fill ARIA radio group '{label}': {e}")
        return False


async def _resolve_checkbox_state(label: str, profile: dict, job_logger) -> tuple[bool, str]:
    """Shared yes/no resolution logic for native and ARIA checkboxes."""
    label_norm = normalize_text(label)

    # --- Permanent hardcoded rules (highest priority, run before everything else) ---

    # Always YES rules
    if any(kw in label_norm for kw in ALWAYS_YES_LABEL_KEYWORDS):
        return True, "hardcoded-rule (always yes)"

    # Always NO rules
    if any(kw in label_norm for kw in ALWAYS_NO_LABEL_KEYWORDS):
        return False, "hardcoded-rule (always no)"

    if is_canada_work_authorization_question(label_norm):
        return True, "profile.json (Canada work-authorization treated as blanket Yes)"

    if is_canada_residency_question(label_norm):
        country = resolve_country_from_location(profile.get("location") or "")
        should_check = country in ("US", "CA")
        return should_check, f"profile.json (location-based: resolved country={country})"

    # Pronouns checkboxes: only check He/Him, uncheck everything else
    pronoun_options = ["he him", "he her", "she her", "they them", "xe xem", "ze zir",
                       "ze hir", "ey em", "hir hir", "fae faer", "hu hu"]
    if label_norm in pronoun_options:
        should_check = label_norm in [normalize_text(p) for p in PRONOUNS_PREFERRED]
        return should_check, "hardcoded-rule (pronouns: He/Him only)"

    classification, matched_key = classify_field(label, "checkbox", profile)
    profile_value = get_profile_value(profile, matched_key)
    should_check = interpret_yes_no(profile_value)
    source = "profile.json"

    if should_check is None:
        # Check if the checkbox is part of a skills / technologies checklist (e.g. Ashby / 1Password)
        candidate_skills = [normalize_text(s) for s in profile.get("skills", []) if s]
        if any(s == label_norm or (len(s) > 2 and s in label_norm) or (len(label_norm) > 2 and label_norm in s) for s in candidate_skills):
            return True, "profile.json (skills match)"

        # No usable profile-derived yes/no signal for this checkbox - fall back
        # to Ollama deciding check/uncheck (still not free text, just a binary state)
        system_prompt = "You are a job application bot. Return 'yes' if the checkbox should be checked or 'no' if not. Be concise."
        prompt = f"""
Candidate profile details:
{json_context_string(profile)}

Checkbox Label:
{label}

Should the candidate check/agree to this checkbox? (Answer 'yes' or 'no' only):
"""
        answer = query_ollama(prompt, system_prompt=system_prompt).lower()
        should_check = "yes" in answer
        source = "ollama"

    return should_check, source


async def fill_checkbox(elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Handles a native <input type=checkbox> (e.g. Terms, equal opportunity, relocation, etc.)."""
    try:
        classification, _ = classify_field(label, "checkbox", profile)
        should_check, source = await _resolve_checkbox_state(label, profile, job_logger)

        # Pronouns: the rule returns False for all non-He/Him options, so
        # explicitly uncheck them if they were pre-selected.
        is_currently_checked = await elem.is_checked()
        if should_check and not is_currently_checked:
            await elem.scroll_into_view_if_needed()
            await elem.check()
        elif not should_check and is_currently_checked:
            await elem.scroll_into_view_if_needed()
            await elem.uncheck()

        log_field_decision(job_logger, label, classification, source, "checked" if should_check else "unchecked")
        return True
    except Exception as e:
        job_logger.error(f"Failed to check checkbox '{label}': {e}")
        return False


async def fill_aria_checkbox(elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Handles a custom-widget [role=checkbox] element (toggled via click + aria-checked, not .check())."""
    try:
        classification, _ = classify_field(label, "checkbox", profile)
        should_check, source = await _resolve_checkbox_state(label, profile, job_logger)

        current_state = (await elem.get_attribute("aria-checked")) == "true"
        if should_check and not current_state:
            await elem.scroll_into_view_if_needed()
            await elem.click()
        elif not should_check and current_state:
            # Explicitly uncheck (e.g. wrong pronoun was pre-selected)
            await elem.scroll_into_view_if_needed()
            await elem.click()

        log_field_decision(job_logger, label, classification, source, "checked" if should_check else "unchecked")
        return True
    except Exception as e:
        job_logger.error(f"Failed to set ARIA checkbox '{label}': {e}")
        return False


async def find_yesno_button_groups(frame, job_logger=None) -> list:
    """
    Detects a common boolean-question UI pattern: two sibling <button>
    elements whose text is exactly "Yes" and "No" under a shared parent
    container (used e.g. by Ashby's "Are you authorized to work in X?"
    fields, often backed by a hidden, non-interactive checkbox that mirrors
    state but isn't the real control). Returns a list of container
    ElementHandles, each holding exactly one Yes button and one No button.
    """
    try:
        array_handle = await frame.evaluate_handle("""() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const seen = new Set();
            const results = [];
            for (const btn of buttons) {
                const text = (btn.textContent || '').trim().toLowerCase();
                if (text !== 'yes' && text !== 'no') continue;
                
                let parent = btn.parentElement;
                let foundContainer = null;
                while (parent && parent !== document.body) {
                    if (seen.has(parent)) break;
                    const innerButtons = Array.from(parent.querySelectorAll('button'));
                    const texts = innerButtons.map(c => (c.textContent || '').trim().toLowerCase());
                    
                    if (innerButtons.length === 2 && texts.includes('yes') && texts.includes('no')) {
                        foundContainer = parent;
                        break;
                    }
                    if (innerButtons.length > 2) break;
                    parent = parent.parentElement;
                }
                
                if (foundContainer) {
                    seen.add(foundContainer);
                    results.push(foundContainer);
                }
            }
            return results;
        }""")
        properties = await array_handle.get_properties()
        containers = []
        for prop in properties.values():
            elem = prop.as_element()
            if elem:
                containers.append(elem)
        return containers
    except Exception as e:
        if job_logger:
            job_logger.warning(f"Yes/No button-group detection failed: {e}")
        else:
            logger.debug(f"Yes/No button-group detection failed: {e}")
        return []


async def is_yesno_already_answered(container: ElementHandle) -> bool:
    """
    True if one button in the pair already carries different state/styling
    than the other (i.e. a selection was already made). These widgets are
    often implemented as toggles rather than radio-style selections, so
    blindly re-clicking on every validation-recovery pass can silently
    de-select an already-correct answer - this check is what prevents that,
    without needing to know any site's specific "selected" class name.
    """
    try:
        classes = await container.evaluate("el => Array.from(el.querySelectorAll('button')).map(b => b.className)")
        return len(set(classes)) > 1
    except Exception:
        return False


async def fill_yesno_buttons(frame, container: ElementHandle, label: str, profile: dict, job_logger, max_attempts: int = 3) -> bool:
    """
    Resolves and clicks the correct button in a Yes/No button-pair question.
    React-based forms can re-render this exact widget shortly after other
    interactions (e.g. a resume-autofill pass triggered by a file upload
    earlier in the same step), detaching the container/button handles we
    already found. Rather than relying solely on the outer validation-
    recovery loop to redo the whole form, retry this one field directly by
    re-querying a fresh container by label on a stale-element failure.
    """
    if await is_yesno_already_answered(container):
        return True

    classification, _ = classify_field(label, "checkbox", profile)
    should_check, source = await _resolve_checkbox_state(label, profile, job_logger)
    target_text = "yes" if should_check else "no"

    current_container = container
    for attempt in range(1, max_attempts + 1):
        try:
            target_btn = None
            for btn in await current_container.query_selector_all("button"):
                text = (await btn.inner_text()).strip().lower()
                if text == target_text:
                    target_btn = btn
                    break
            if not target_btn:
                return False

            await target_btn.scroll_into_view_if_needed()
            await target_btn.click()
            log_field_decision(job_logger, label, classification, source, "Yes" if should_check else "No")
            return True
        except Exception as e:
            if attempt == max_attempts:
                job_logger.error(f"Failed to set Yes/No buttons '{label}' after {max_attempts} attempts: {e}")
                return False

            job_logger.warning(f"Yes/No buttons '{label}' hit a transient error (attempt {attempt}/{max_attempts}), re-querying and retrying: {e}")
            await asyncio.sleep(0.4)

            fresh_groups = await find_yesno_button_groups(frame, job_logger)
            for g in fresh_groups:
                if await get_field_label(frame, g) == label:
                    current_container = g
                    break
    return False


async def handle_file_upload(elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """
    Uploads the resume file if the field is for resume/CV. Deliberately does not
    require the input to be visible - drag-and-drop uploaders commonly hide the
    real <input type=file> behind a styled dropzone, and set_input_files works
    on hidden inputs regardless.
    """
    try:
        label_lower = label.lower()
        
        # Fallback: if the visible label is generic (like "Drop or select"),
        # check underlying DOM attributes for hints like "data-testid='resume'"
        if not any(term in label_lower for term in ["resume", "cv", "curriculum", "cover letter"]):
            dom_hints = await elem.evaluate("""el => {
                let lbl = el.closest('label');
                return [
                    el.id, el.name, el.getAttribute('data-testid'), el.getAttribute('aria-label'),
                    lbl ? lbl.getAttribute('data-testid') : '',
                    lbl ? lbl.getAttribute('aria-label') : ''
                ].join(' ').toLowerCase();
            }""")
            label_lower += " " + dom_hints

        if "cover letter" in label_lower:
            try:
                cover_letter_path = get_or_create_cover_letter(profile, job_logger)
            except Exception as e:
                job_logger.error(f"Failed to generate cover letter for field '{label}': {e}")
                return False
            absolute_path = os.path.abspath(cover_letter_path)
            await elem.set_input_files(absolute_path)
            job_logger.info(f"Uploaded generated cover letter '{absolute_path}' to field '{label}'")
            return True
        elif any(term in label_lower for term in ["resume", "cv", "curriculum vitae"]):
            resume_path = profile.get("resume_file_path", "")
            if resume_path and os.path.exists(resume_path):
                absolute_path = os.path.abspath(resume_path)
                await elem.set_input_files(absolute_path)
                job_logger.info(f"Uploaded resume file '{absolute_path}' to field '{label}'")
                return True
            else:
                job_logger.error(f"Resume file path '{resume_path}' is invalid or file does not exist.")
        else:
            job_logger.info(f"Skipped file upload field '{label}' (not a resume/CV/cover letter upload)")
            return True
    except Exception as e:
        job_logger.error(f"Failed to upload file to field '{label}': {e}")
    return False


async def find_and_click_next_button(frame, fields_found: int = 1) -> bool:
    """
    Searches for multi-step buttons like 'Next', 'Continue', 'Proceed', 'Step'
    and clicks them if found. Returns True if button was clicked.
    """
    next_selectors = [
        "button:has-text('Next')", "button:has-text('Continue')",
        "button:has-text('Proceed')", "input[type='button'][value='Next']",
        "input[type='button'][value='Continue']", "a:has-text('Next')",
        "button[id*='next']", "button[class*='next']",
        "button:has-text('Apply for this job')", "a:has-text('Apply for this job')",
        "a:has-text('Apply Now')", "button:has-text('Apply Now')",
        "a:has-text('Apply To Position')", "button:has-text('Apply To Position')",
        "a:has-text('Apply')", "button:has-text('Apply')",
        "button:has-text('I Confirm')", "a:has-text('I Confirm')", "input[value='I Confirm']",
        "button:has-text('I Accept')", "a:has-text('I Accept')", "input[value='I Accept']",
        "button:has-text('Agree')", "a:has-text('Agree')", "input[value='Agree']"
    ]
        
    for selector in next_selectors:
        try:
            btn = frame.locator(selector).first
            if await btn.is_visible() and await btn.is_enabled():
                # Make sure it's not a submit button (unless it's the last page)
                btn_type = await btn.get_attribute("type")
                btn_text = await btn.inner_text()
                if btn_type == "submit" and any(term in btn_text.lower() for term in ["submit", "apply", "finish"]):
                    continue # Let the final submit block handle it

                await btn.scroll_into_view_if_needed()
                await btn.click()
                logger.info(f"Clicked next step button: '{btn_text or selector}'")
                await asyncio.sleep(0.5)
                return True
        except Exception:
            continue
    return False


async def find_validation_problems(frame) -> tuple[bool, list[str]]:
    """
    Generic, framework-agnostic validation scan: aria-invalid state, empty
    required fields, native HTML5 invalidity, and visible error-styled text.
    """
    reasons = []

    try:
        invalid_elems = await frame.query_selector_all("[aria-invalid='true']")
        for elem in invalid_elems:
            if await elem.is_visible():
                label = await get_field_label(frame, elem)
                reasons.append(f"Field '{label or 'unknown'}' marked aria-invalid")
    except Exception:
        pass

    try:
        required_elems = await frame.query_selector_all("input[required]:not([type='hidden']), select[required], textarea[required]")
        for elem in required_elems:
            if await elem.is_visible():
                value = await elem.evaluate("el => el.value")
                if not value or not str(value).strip():
                    label = await get_field_label(frame, elem)
                    reasons.append(f"Required field '{label or 'unknown'}' is empty")
    except Exception:
        pass

    try:
        error_elems = await frame.query_selector_all("[class*='error'], [class*='invalid']")
        for elem in error_elems:
            if await elem.is_visible():
                has_inputs = await elem.evaluate("el => el.querySelector('input, select, textarea, button, a, li, label, [role]') !== null")
                if not has_inputs:
                    text = (await elem.inner_text()).strip()
                    if text and len(text) < 200 and "\n" not in text:
                        reasons.append(f"Visible error message: '{text}'")
    except Exception:
        pass

    seen = set()
    unique_reasons = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique_reasons.append(r)

    return (len(unique_reasons) > 0), unique_reasons[:10]





async def process_form_fields(frame, profile: dict, job_logger) -> int:
    """
    Enumerates every field type in the current step and fills them in a single
    pass ordered by on-page vertical position - top to bottom. Using a unified
    selector guarantees that if bounding box detection fails, elements fall back
    to their strict DOM order (which matches visual top-to-bottom layout), rather
    than grouping by field type.
    """
    tasks = []  # (discovery_order, kind, elem, extra)
    order_counter = 0

    async def _add(elem, kind, extra=None):
        nonlocal order_counter
        order_counter += 1
        tasks.append((order_counter, kind, elem, extra))

    giant_selector = (
        "input:not([type='hidden']):not([type='submit']):not([type='button']), "
        "input[type='file'], "
        "textarea, [contenteditable='true'], select, [role='combobox'], "
        "[role='radiogroup'], [role='checkbox']"
    )
    
    elems = await frame.query_selector_all(giant_selector)
    processed_radio_names = set()

    for elem in elems:
        try:
            if await is_chat_or_support_element(elem):
                continue
            tag = await elem.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            continue
            
        role = (await elem.get_attribute("role") or "").lower()
        type_attr = (await elem.get_attribute("type") or "").lower()
        contenteditable = (await elem.get_attribute("contenteditable") or "").lower()

        if tag == "input" and type_attr == "radio":
            if await elem.is_visible():
                name_attr = await elem.get_attribute("name")
                if name_attr and name_attr not in processed_radio_names:
                    processed_radio_names.add(name_attr)
                    await _add(elem, "radio", name_attr)
        elif tag == "input" and type_attr == "checkbox":
            if await elem.is_visible():
                await _add(elem, "checkbox")
        elif tag == "input" and type_attr == "file":
            if await elem.is_visible():
                await _add(elem, "file")
        elif role == "radiogroup":
            if await elem.is_visible():
                await _add(elem, "radiogroup")
        elif role == "checkbox" and tag != "input":
            if await elem.is_visible():
                await _add(elem, "aria_checkbox")
        elif role == "combobox" and tag not in ("input", "textarea", "select"):
            if await elem.is_visible():
                await _add(elem, "combobox")
        elif tag == "select":
            if await elem.is_visible():
                await _add(elem, "select")
        elif tag == "textarea" or contenteditable == "true" or (tag == "input" and type_attr not in ("radio", "checkbox", "file", "hidden", "submit", "button")):
            if await elem.is_visible():
                await _add(elem, "text")

    yesno_groups = await find_yesno_button_groups(frame, job_logger)
    job_logger.info(f"Detected {len(yesno_groups)} Yes/No button-pair field(s) on this step.")
    for container in yesno_groups:
        try:
            if await container.is_visible():
                if await is_chat_or_support_element(container):
                    continue
                await _add(container, "yesno")
        except Exception:
            continue

    # Single top-to-bottom pass, ordered strictly by DOM discovery order.
    tasks.sort(key=lambda t: t[0])

    for _, kind, elem, extra in tasks:
        try:
            if kind == "text":
                label = await get_field_label(frame, elem)
                await fill_text_field(frame, elem, label, profile, job_logger)
            elif kind == "select":
                label = await get_field_label(frame, elem)
                await fill_select_field(elem, label, profile, job_logger)
            elif kind == "combobox":
                label = await get_field_label(frame, elem)
                _, matched_key = classify_field(label, "combobox", profile)
                await handle_combobox_field(frame, elem, label, profile, job_logger, matched_key)
            elif kind == "file":
                label = await get_field_label(frame, elem)
                await handle_file_upload(elem, label, profile, job_logger)
                # Some ATS forms (e.g. Ashby's "Autofill from resume") re-parse the
                # upload and re-render parts of the form afterward - give that a
                # moment to settle before touching whatever comes next, so we
                # don't act on element handles that are about to be replaced.
                await wait_for_fields_to_settle(frame, timeout_ms=2000)
            elif kind == "radio":
                label = await get_field_label(frame, elem, is_group=True)
                await fill_radio_group(frame, extra, label, profile, job_logger)
            elif kind == "radiogroup":
                group_label = (await elem.get_attribute("aria-label")) or await get_field_label(frame, elem, is_group=True)
                await fill_aria_radio_group(frame, elem, group_label, profile, job_logger)
            elif kind == "checkbox":
                label = await get_field_label(frame, elem)
                await fill_checkbox(elem, label, profile, job_logger)
            elif kind == "aria_checkbox":
                label = await get_field_label(frame, elem)
                await fill_aria_checkbox(elem, label, profile, job_logger)
            elif kind == "yesno":
                label = await get_field_label(frame, elem, is_group=True)
                await fill_yesno_buttons(frame, elem, label, profile, job_logger)
        except Exception as e:
            # A field earlier in this same pass (e.g. a file upload triggering
            # an autofill re-render) can detach elements discovered before it.
            # The outer validation-recovery loop in fill_and_submit_form
            # re-scans the whole form afterward, so skip and move on rather
            # than aborting the whole pass.
            job_logger.warning(f"Skipping a '{kind}' field mid-pass due to an error (likely a stale element from a re-render earlier in this pass): {e}")

    return len(tasks)


# ---------------------------------------------------------------------------
# Submission success detection
# ---------------------------------------------------------------------------

SUCCESS_TEXT_PATTERNS = [
    "thank you for applying", "thank you for your application",
    "application submitted", "application received",
    "we've received your application", "we have received your application",
    "successfully submitted", "your application has been submitted",
    "application complete", "submission successful",
]


async def detect_submission_success(page, frame, pre_submit_url: str) -> bool:
    """
    Decides whether a submission actually succeeded: a URL change away from the
    form, or a confirmation message (checked on both the top-level page and the
    active frame, since embedded forms may show their confirmation inside the
    iframe itself), counts as success.
    """
    p_page = _get_raw_playwright_page(page)

    # Helper: safe URL parse
    try:
        from urllib.parse import urlparse
        pre = urlparse(pre_submit_url)
        pre_path = pre.path.rstrip('/') if pre and pre.path else ""
    except Exception:
        pre = None
        pre_path = ""

    # 1) Check for a newly opened page/tab that differs from the pre-submit URL
    try:
        ctx_pages = list(p_page.context.pages)
        for pg in ctx_pages:
            if pg is p_page:
                continue
            try:
                if pre:
                    post = urlparse(pg.url)
                    post_path = post.path.rstrip('/') if post and post.path else ""
                    if post.netloc != pre.netloc or post_path != pre_path:
                        return True
                title = (await pg.title()).lower()
                for pattern in SUCCESS_TEXT_PATTERNS:
                    if pattern in title:
                        return True
            except Exception:
                continue
    except Exception:
        pass

    # 2) Check main page URL change (unchanged logic, but more permissive)
    try:
        if pre:
            post = urlparse(p_page.url)
            post_path = post.path.rstrip('/') if post and post.path else ""
            if post.netloc != pre.netloc or post_path != pre_path:
                if not (post_path.endswith('/application') or post_path.endswith('/apply')):
                    return True
    except Exception:
        pass

    # 3) Check page title for success phrases
    try:
        title = (await p_page.title()).lower()
        for pattern in SUCCESS_TEXT_PATTERNS:
            if pattern in title:
                return True
    except Exception:
        pass

    # 4) Check the text content of the top-level page and all frames
    try:
        scopes = [p_page] + list(p_page.frames)
        for scope in scopes:
            try:
                body_text = (await scope.inner_text("body")).lower()
                for pattern in SUCCESS_TEXT_PATTERNS:
                    if pattern in body_text:
                        return True
            except Exception:
                continue
    except Exception:
        pass

    # 5) Check for common semantic success elements (alerts, status, thank-you classes)
    success_selectors = [".application-confirmation", ".thank-you", ".submitted", "text=thank you", "text=application received"]
    try:
        for sel in success_selectors:
            try:
                loc = p_page.locator(sel)
                cnt = await loc.count()
                if cnt > 0:
                    for i in range(cnt):
                        if await loc.nth(i).is_visible():
                            return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _save_screenshot(page: Page, company: str, job_link: str, job_logger, prefix: str):
    """Saves a full-page screenshot to SCREENSHOTS_DIR/<Company>/<job_link_slug>/<prefix>_<timestamp>.png"""
    try:
        from modules.logger import clean_filename
        link_slug = clean_filename(job_link) or "unknown_job"
        company_dir = os.path.join(config.SCREENSHOTS_DIR, company, link_slug)
        os.makedirs(company_dir, exist_ok=True)
        screenshot_name = f"{prefix}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.png"
        screenshot_path = os.path.join(company_dir, screenshot_name)
        await page.screenshot(path=screenshot_path, full_page=True)
        job_logger.info(f"Screenshot saved to: {screenshot_path}")
    except Exception as e:
        job_logger.warning(f"Saving screenshot '{prefix}' failed: {e}")


async def fill_and_submit_form(page: Page, profile: dict, job_logger, company: str, job_link: str, dry_run: bool = False) -> tuple[str, str]:
    """
    Main orchestration loop for navigating and filling a single job application.
    Returns (status, reason). status is one of "Submitted", "Failed",
    "Human Attention", or "Dry Run" (only when dry_run=True).
    """
    try:
        # Step 1: Navigated page captcha/login check
        has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
        if has_captcha:
            return "Human Attention", captcha_reason

        p_page = _get_raw_playwright_page(page)

        # Early check for expired / closed / 404 job postings
        is_expired, expired_reason = await detect_expired_or_missing_job(p_page)
        if is_expired:
            job_logger.error(f"Job is no longer open: {expired_reason}")
            return "Expired", f"Job posting expired / closed / 404: {expired_reason}"

        # Wait for any embedded ATS (e.g. Greenhouse/Lever on Webflow or custom domains)
        await wait_for_embedded_ats(p_page, job_logger)

        # Step 1.5: If we landed on a Job Description page instead of a form, click "Apply".
        # We don't rely on fields_count because Taleo and others have search bars that inflate the count.
        apply_selectors = [
            "[data-automation-id='jobFoundationalApplyButton']",
            "[data-automation-id='adventureButton']",
            "a:has-text('Apply Manually')",
            "button:has-text('Apply Manually')",
            "#ApplyOnline",
            "a[id*='ApplyOnline']",
            "[title='Apply Online']",
            "a[data-ph-at-id='apply-button']",
            "[data-ph-id*='apply']",
            "a[data-test='apply-button']",
            "button:has-text('Apply Now')",
            "button:has-text('Apply Online')",
            "a:has-text('Apply Now')",
            "a:has-text('Apply Online')",
            "button:has-text('Apply for this job')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply to job')",
            "a:has-text('Apply to job')",
            "input[value='Apply Online']",
            "input[value='Apply Now']",
            "input[value='Apply']",
            "a.btn-apply",
            "button:has-text('Apply'):not([id*='filter']):not([class*='ot-'])",
            "a:has-text('Apply'):not([id*='filter']):not([class*='ot-'])"
        ]
        
        clicked_initial_apply = False
        for f in p_page.frames:
            if is_chat_frame(f):
                continue
            if clicked_initial_apply:
                break
            for sel in apply_selectors:
                try:
                    btn = f.locator(sel).first
                    if await btn.is_visible() and await btn.is_enabled():
                        await btn.scroll_into_view_if_needed()

                        # Capture any new tab that the Apply button opens
                        # (target="_blank" links).  We set up a one-shot popup
                        # listener BEFORE the click, then switch the bot's page
                        # reference to the popup if one appears within ~3 s.
                        _popup_page = None
                        async def _on_popup(new_page):
                            nonlocal _popup_page
                            _popup_page = new_page
                        p_page.context.once("page", _on_popup)

                        await btn.click(timeout=3000)
                        job_logger.info(f"Clicked initial 'Apply' button on job description page: {sel}")
                        clicked_initial_apply = True

                        # Wait briefly to see if a popup was spawned
                        try:
                            await asyncio.sleep(1.5)
                        except Exception:
                            pass

                        if _popup_page is not None:
                            # A new tab opened — follow it and close the old one
                            try:
                                await _popup_page.wait_for_load_state("domcontentloaded", timeout=10000)
                            except Exception:
                                pass
                            job_logger.info(f"Apply button opened a new tab; switching to it: {_popup_page.url}")
                            p_page = _popup_page
                            # Replace the wrapped page reference that fill_and_submit_form holds
                            if hasattr(page, '_page'):
                                page._page = _popup_page
                            elif hasattr(page, 'page'):
                                page.page = _popup_page
                        else:
                            try:
                                await p_page.wait_for_load_state("networkidle", timeout=5000)
                            except Exception:
                                pass

                        await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=10000)
                        break
                except Exception:
                    continue

        # Step 2: Loop to handle single or multi-page forms
        max_steps = 5
        frame = p_page.main_frame
        total_fields_filled_across_steps = 0
        for step in range(1, max_steps + 1):
            job_logger.info(f"Processing Form Step {step}...")

            # Recheck CAPTCHA at each step
            has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
            if has_captcha:
                return "Human Attention", captcha_reason

            await wait_for_fields_to_settle(p_page.main_frame)
            frame = await select_active_frame(p_page)

            fields_found = await process_form_fields(frame, profile, job_logger)
            total_fields_filled_across_steps += fields_found

            # Validation-error recovery loop: re-attempt filling up to twice more
            for recovery_pass in range(1, MAX_VALIDATION_RECOVERY_PASSES + 1):
                has_errors, reasons = await find_validation_problems(frame)
                if not has_errors:
                    break
                job_logger.warning(f"Validation issues detected (pass {recovery_pass}): {reasons}. Re-attempting fill...")
                extra = await process_form_fields(frame, profile, job_logger)
                total_fields_filled_across_steps += extra

            has_errors, reasons = await find_validation_problems(frame)
            if has_errors:
                job_logger.warning(f"Unresolved validation errors after retries in step {step}: {reasons}. Will attempt to proceed or re-evaluate instead of failing immediately.")

            # Step 1 Zero-Field Safety Guard:
            if step == 1 and total_fields_filled_across_steps == 0:
                # If we filled 0 fields on step 1, check if an Apply button was missed
                clicked_late_apply = False
                for f in p_page.frames:
                    if is_chat_frame(f):
                        continue
                    for sel in apply_selectors:
                        try:
                            btn = f.locator(sel).first
                            if await btn.is_visible() and await btn.is_enabled():
                                await btn.scroll_into_view_if_needed()
                                await btn.click(timeout=3000)
                                job_logger.info(f"Clicked late 'Apply' button on page: {sel}")
                                clicked_late_apply = True
                                await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=10000)
                                break
                        except Exception:
                            continue
                    if clicked_late_apply:
                        break

                if clicked_late_apply:
                    frame = await select_active_frame(p_page)
                    extra = await process_form_fields(frame, profile, job_logger)
                    total_fields_filled_across_steps += extra

                if total_fields_filled_across_steps == 0:
                    job_logger.warning("No form fields detected on Step 1. Waiting up to 60 seconds for a form to load or for manual intervention to open the form...")
                    for _ in range(30):
                        await asyncio.sleep(2)
                        frame = await select_active_frame(p_page)
                        extra = await process_form_fields(frame, profile, job_logger)
                        if extra > 0:
                            total_fields_filled_across_steps += extra
                            job_logger.info("Form fields appeared! Resuming automation.")
                            break

                    if total_fields_filled_across_steps == 0:
                        job_logger.error("Still no form fields detected after 60 seconds. Aborting.")
                        return "Failed", "No fillable application form fields detected on page"

            # Check if there is a next step
            clicked_next = await find_and_click_next_button(frame, fields_found)
            if not clicked_next:
                # No next button found, assume we are on the final step
                job_logger.info("No next button found. Form filling complete.")
                break
            await wait_for_fields_to_settle(p_page.main_frame)

        # Step 3: Pre-submit validation
        has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
        if has_captcha:
            return "Human Attention", captcha_reason

        frame = await select_active_frame(p_page)

        if dry_run:
            job_logger.info("Dry run enabled - form filled but stopping before the final submit click.")
            return "Dry Run", "Dry run mode - form filled but not submitted"

        # Step 4: Submission
        if config.AUTO_SUBMIT:
            for submit_attempt in range(1, 4):
                try:
                    pre_submit_url = p_page.url
                except Exception:
                    pre_submit_url = ""

                # Check if we accidentally already submitted the form (e.g. if a "Next" or "Apply" button 
                # clicked during the form loop was actually the final submit button).
                if await detect_submission_success(page, frame, pre_submit_url):
                    job_logger.info("Application already submitted successfully during the navigation loop.")
                    if config.SCREENSHOT_ON_SUCCESS:
                        await _save_screenshot(p_page, company, job_link, job_logger, "applied_success")
                    return "Submitted", ""

                job_logger.info(f"Waiting 5 seconds before final submission (attempt {submit_attempt})...")
                import asyncio
                await asyncio.sleep(5)

                submit_selectors = [
                    "button[type='submit']", "input[type='submit']",
                    "button:has-text('Submit Application')", "button:has-text('Submit')",
                    "input[type='button'][value='Submit Application']", "input[type='button'][value='Submit']",
                    "button:has-text('Apply'):not([id*='filter']):not([class*='ot-'])",
                    "input[type='button'][value='Apply']"
                ]

                submitted = False
                for selector in submit_selectors:
                    try:
                        btn = frame.locator(selector).first
                        if await btn.is_visible() and await btn.is_enabled():
                            if await is_chat_or_support_element(btn):
                                continue
                            await btn.scroll_into_view_if_needed()
                            await _save_screenshot(p_page, company, job_link, job_logger, "before_submit")
                            await btn.click()
                            job_logger.info(f"Clicked submit button matching selector: '{selector}'")
                            submitted = True
                            break
                    except Exception:
                        continue
                # Fallback 1: Try role/button/anchor matches for 'Apply' or other text-based buttons
                if not submitted:
                    extra_text_selectors = [
                        "[role='button']:has-text('Submit Application')", "[role='button']:has-text('Submit')",
                        "a:has-text('Submit Application')", "a:has-text('Submit')",
                        "[role='button']:has-text('Apply'):not([id*='filter']):not([class*='ot-'])",
                        "a:has-text('Apply'):not([id*='filter']):not([class*='ot-'])",
                    ]
                    try:
                        frames_to_try = [frame] + [f for f in p_page.frames if f is not frame and not is_chat_frame(f)]
                        for scope in frames_to_try:
                            for sel in extra_text_selectors:
                                try:
                                    loc = scope.locator(sel).first
                                    if await loc.count() and await loc.is_visible() and await loc.is_enabled():
                                        if await is_chat_or_support_element(loc):
                                            continue
                                        await loc.scroll_into_view_if_needed()
                                        await _save_screenshot(p_page, company, job_link, job_logger, "before_submit")
                                        await loc.click()
                                        job_logger.info(f"Clicked submit-like element matching selector: '{sel}'")
                                        submitted = True
                                        break
                                except Exception:
                                    continue
                            if submitted:
                                break
                    except Exception:
                        pass

                # Fallback 2: Programmatically submit the <form> element if present and fields were actually filled
                if not submitted and total_fields_filled_across_steps > 0:
                    try:
                        frm = await frame.query_selector("form")
                        if frm and not await is_chat_or_support_element(frm):
                            try:
                                await frm.evaluate("f => (f.requestSubmit ? f.requestSubmit() : f.submit())")
                                job_logger.info("Submitted using form.requestSubmit()/form.submit()")
                                submitted = True
                            except Exception:
                                pass
                    except Exception:
                        pass

                if not submitted:
                    # Fallback 3: run an in-page JS search that traverses shadowRoots and iframes
                    try:
                        js_clicker = '''(function(){
    function isVisible(el){
        if(!el) return false;
        var style = window.getComputedStyle(el);
        if(!style) return false;
        if(style.visibility==='hidden' || style.display==='none' || parseFloat(style.opacity||1)===0) return false;
        var r = el.getBoundingClientRect();
        return !!(r.width && r.height);
    }
    function findAndClick(root){
        var list = [];
        function visit(node){
            if(node.nodeType!==1) return;
            try{
                var text = (node.innerText||'').trim().toLowerCase();
                if(isVisible(node) && (text.indexOf('apply')!==-1 || text.indexOf('submit')!==-1)){
                    list.push(node);
                }
            }catch(e){}
            try{ if(node.shadowRoot) visit(node.shadowRoot.host); }catch(e){}
            for(var i=0;i<node.children.length;i++) visit(node.children[i]);
        }
        visit(root.documentElement||root);
        if(list.length){
            for(var j=0;j<list.length;j++){
                try{ list[j].click(); return true;}catch(e){}
            }
        }
        return false;
    }
    try{ if(findAndClick(document)) return true;}catch(e){}
    var iframes = document.querySelectorAll('iframe');
    for(var k=0; k<iframes.length; k++){
        try{
            var doc = iframes[k].contentDocument;
            if(doc && findAndClick(doc)) return true;
        }catch(e){}
    }
    return false;
})();'''

                        clicked = await frame.evaluate(js_clicker)
                        if clicked:
                            job_logger.info("Clicked submit-like element via JS fallback")
                            submitted = True
                    except Exception:
                        pass

                if not submitted:
                    # Save a diagnostic screenshot and the page HTML to help debugging
                    try:
                        await _save_screenshot(p_page, company, job_link, job_logger, "no_submit")
                    except Exception:
                        pass
                    try:
                        content = await p_page.content()
                        from modules.logger import clean_filename
                        link_slug = clean_filename(job_link) or "unknown_job"
                        company_dir = os.path.join(config.SCREENSHOTS_DIR, company, link_slug)
                        os.makedirs(company_dir, exist_ok=True)
                        html_path = os.path.join(company_dir, "no_submit_page.html")
                        with open(html_path, "w", encoding="utf-8") as fh:
                            fh.write(content)
                        job_logger.info(f"Saved page HTML to: {html_path}")
                    except Exception as e:
                        job_logger.warning(f"Saving page HTML failed: {e}")

                    return "Failed", "Could not locate visible submit button on the final page"

                # Wait for response/navigation - bounded settle-wait plus the existing
                # network-idle wait, instead of a blind fixed sleep
                job_logger.info("Waiting 5 seconds after form submission as requested...")
                import asyncio
                await asyncio.sleep(5)
                
                try:
                    await p_page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=5000)

                await _save_screenshot(p_page, company, job_link, job_logger, "after_submit")

                if await detect_submission_success(page, frame, pre_submit_url):
                    job_logger.info("Application form submitted successfully (confirmation detected).")
                    if config.SCREENSHOT_ON_SUCCESS:
                        await _save_screenshot(p_page, company, job_link, job_logger, "applied_success")
                    return "Submitted", ""
                else:
                    # Check if a CAPTCHA or login wall appeared as a result of clicking submit
                    has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
                    if has_captcha:
                        job_logger.warning(f"Submission blocked: {captcha_reason}")
                        return "Human Attention", f"Submission blocked by {captcha_reason}"
                    
                    # NEW: Check for validation errors after clicking submit
                    has_errors, reasons = await find_validation_problems(frame)
                    if has_errors:
                        job_logger.warning(f"Validation errors appeared after submit (attempt {submit_attempt}): {reasons}. Attempting to fill missing fields.")
                        await process_form_fields(frame, profile, job_logger)
                        continue  # Loop back and try submitting again

                    job_logger.warning("Submit button was clicked but no confirmation (URL change or success message) was detected.")
                    return "Failed", "No confirmation of submission detected after clicking submit"
            
            return "Failed", "Exceeded maximum submission attempts due to recurring validation errors."
        else:
            job_logger.info("Auto-submit is disabled. Skipping submission click.")
            return "Submitted", "Auto-submit disabled (manual review mode)"

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        job_logger.error(f"Exception occurred during form filling: {e}\nTraceback:\n{tb}")
        return "Failed", f"Exception: {e}"
