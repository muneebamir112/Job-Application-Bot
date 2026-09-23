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

# Placeholder option texts that indicate a <select> has no real value selected.
# Used to skip the early-exit in fill_select_field and to filter fallback choices.
_SELECT_PLACEHOLDER_TEXTS = {
    "select", "select...", "select one", "select one...", "please select",
    "please select one", "choose", "choose one", "choose one...", "none",
    "none selected", "-- select --", "- select -", "---", "--", "-"
}

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
    # secondary_last_name MUST come before last_name — "secondary last name" contains "last name"
    # so if last_name is checked first it would steal the match. We return None for this key
    # so that secondary/maiden name fields are always left blank.
    "secondary_last_name": ["secondary last name", "second last name", "second surname", "maiden name", "previous last name", "former last name"],
    "last_name": ["last name", "surname", "family name"],
    "full_name": ["full name", "your name", "candidate name", "applicant name", "legal name", "name", "enter your name", "what is your name"],
    "email": ["email", "e-mail", "email address", "e-mail address"],
    "phone": ["phone", "mobile", "telephone", "phone number", "contact number"],
    "work_authorization": [
        "work authorization", "authorized to work", "legal authorization", "right to work", "employment authorization",
        "legally authorized to work", "authorized to work in the country", "legally authorized to work in the country"
    ],
    "visa_sponsorship_needed": [
        "visa", "sponsorship", "sponsor", "sponorship", "sponor", "require visa", "need sponsorship", "require sponsorship",
        "employment sponsorship", "employment-based visas", "visa sponsorship",
        "require employment sponsorship", "provide employment sponsorship",
        "require the support of", "maintain that authorization", "support to maintain", "maintain authorization"
    ],
    "city": [
        "city", "current city", "city of residence", "list your city", "what city", "which city",
        "please list your city", "please list your city of residence", "residence city", "your city"
    ],
    "state": [
        "state", "province", "region", "state / province", "state/province",
        "which state", "what state", "state do you reside", "state you reside",
        "which state do you reside", "which state do you reside in", "what state do you reside",
        "state of residence", "residence state", "current state", "your state"
    ],
    "location": [
        "location", "current location", "residence",
        "address", "where are you based", "based in"
    ],
    "country": ["country", "country/region", "country of residence"],
    "linkedin": ["linkedin", "linked in", "linkedin url", "linkedin profile"],
    "github": ["github", "git hub", "github url", "github profile"],
    "portfolio": ["portfolio", "website", "personal website", "blog", "portfolio url"],
    "twitter": ["twitter", "twitter url", "twitter profile", "x profile", "twitter x", "twitter(x)", "x url", "x account", "twitter handle", "x handle", "x (fka twitter)", "x fka twitter"],
    "facebook": ["facebook", "facebook url", "facebook profile", "facebook username", "facebook handle", "facebook link", "fb", "fb profile"],
    "instagram": ["instagram", "instagram url", "instagram profile", "instagram username", "instagram handle", "instagram link", "ig"],
    "youtube": ["youtube", "youtube url", "youtube profile", "youtube channel", "youtube link"],
    "current_title": ["current title", "headline", "job title", "current role", "current position"],
    "current_company": ["current company", "employer name"],
    "years_experience": ["years of experience", "experience level", "experience years", "years exp", "total experience"],
    "salary_expectation": ["salary", "compensation", "expected salary", "salary expectation", "desired salary"],
    "notice_period": ["notice period", "notice", "availability", "start date", "when can you start"],
    "willing_to_relocate": ["relocate", "relocation", "willing to relocate"],
    "remote_work": ["open to remote", "remote work preference", "willing to work remote", "work remotely", "prefer remote"],
    "golang_experience": ["golang", "go language", "experience in golang", "golang experience", "experience with golang", "professional development experience in golang"],
    "timezone": [
        "time zone", "timezone", "us time zone", "what time zone", "which time zone",
        "your time zone", "what us time zone", "which us time zone", "time zone are you located in",
        "us time zone are you located in", "what us time zone are you located in",
        "what time zone are you located in", "what time zone are you in", "which time zone are you in",
        "time zone you are located in", "us time zone you are located in"
    ],
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

    words = label_norm.split()
    # If the label is an open-ended question / prompt rather than a standard profile field,
    # skip single-token profile matches to avoid false positives (e.g. "stateful" -> "state").
    is_open_ended_prompt = (
        len(words) > 4
        and any(
            p in label_norm
            for p in [
                "explain", "describe", "why do you", "why are you", "tell us",
                "how do you", "how would you", "what experience", "what projects",
                "what is your experience", "share an example", "what makes you"
            ]
        )
    )

    for key, keywords in PROFILE_FIELD_SYNONYMS.items():
        if is_open_ended_prompt and key in (
            "state", "country", "location", "first_name", "last_name", "middle_name",
            "full_name", "phone", "email", "gender", "race", "notice_period", "willing_to_relocate",
            "facebook", "twitter", "instagram", "youtube", "github"
        ):
            continue

        for kw in keywords:
            kw_norm = normalize_text(kw)
            if not kw_norm:
                continue

            # Exact phrase match
            if kw_norm == label_norm:
                return key

            # Word-boundary regex matching to prevent matching inside other words (e.g. "state" in "stateful")
            pattern = r'\b' + re.escape(kw_norm) + r'\b'
            if re.search(pattern, label_norm):
                # If a single-word keyword appears inside a long question (> 4 words), ignore it unless it's a known multi-word phrase
                if len(kw_norm.split()) == 1 and len(words) > 4 and key in (
                    "state", "country", "location", "notice_period", "willing_to_relocate", "gender", "race",
                    "facebook", "twitter", "instagram", "youtube", "github"
                ):
                    continue
                return key

    # Fallbacks for common placeholders when there are no labels
    if "example com" in label_norm or "name example" in label_norm or " domain " in label_norm or label_norm.startswith("email "):
        return "email"
    if "415 555" in label_norm or "555 1234" in label_norm or "555 5555" in label_norm or "123 456" in label_norm:
        return "phone"

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
        # Textareas should never be populated with simple single-property fields like 'state' or 'country'
        if field_type == "textarea" and matched_key in ("state", "country", "first_name", "last_name", "phone", "email", "gender", "race"):
            return "OPEN_ENDED", None
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

    # --- secondary_last_name: secondary/maiden/former name — intentionally left blank ---
    # "Secondary Last Name" is a supplemental field on some ATS forms (e.g. SmartRecruiters)
    # that refers to a maiden name or alias. We never have this in profile and must leave blank.
    if key == "secondary_last_name":
        return None

    if key == "twitter":
        return profile.get("twitter") or profile.get("Twitter") or profile.get("Twitter(X)") or profile.get("twitter_url") or profile.get("x")

    if key == "facebook":
        return profile.get("facebook") or profile.get("Facebook") or profile.get("facebook_url") or profile.get("facebook_profile") or profile.get("facebook_username") or None

    if key == "instagram":
        return profile.get("instagram") or profile.get("Instagram") or profile.get("instagram_url") or profile.get("instagram_profile") or profile.get("instagram_username") or None

    if key == "youtube":
        return profile.get("youtube") or profile.get("YouTube") or profile.get("youtube_url") or profile.get("youtube_profile") or None

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

    if key == "city":
        city_val = profile.get("city")
        if city_val:
            return str(city_val).strip()
        loc = profile.get("location") or ""
        if "," in loc:
            return loc.split(",")[0].strip()
        return loc.strip() if loc else None

    if key == "state":
        state_val = profile.get("state")
        if state_val:
            s = str(state_val).strip()
            return US_STATE_CODE_TO_NAME.get(s.lower(), s)
        loc = profile.get("location") or ""
        if "," in loc:
            raw_state = loc.split(",")[1].strip()
            return US_STATE_CODE_TO_NAME.get(raw_state.lower(), raw_state)
        return None

    if key == "country":
        c = resolve_country_from_location(profile.get("location") or "")
        if c == "US":
            return "United States"
        elif c == "CA":
            return "Canada"
        elif c in ("UK", "GB"):
            return "United Kingdom"
        return c

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

US_STATE_CODE_TO_NAME = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas", "ca": "California",
    "co": "Colorado", "ct": "Connecticut", "de": "Delaware", "fl": "Florida", "ga": "Georgia",
    "hi": "Hawaii", "id": "Idaho", "il": "Illinois", "in": "Indiana", "ia": "Iowa",
    "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota", "ms": "Mississippi", "mo": "Missouri",
    "mt": "Montana", "ne": "Nebraska", "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey",
    "nm": "New Mexico", "ny": "New York", "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio",
    "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island", "sc": "South Carolina",
    "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas", "ut": "Utah", "vt": "Vermont",
    "va": "Virginia", "wa": "Washington", "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
    "dc": "Washington, D.C.",
}

US_STATE_NAME_TO_CODE = {v.lower(): k for k, v in US_STATE_CODE_TO_NAME.items()}
US_STATE_NAME_TO_CODE["district of columbia"] = "dc"
US_STATE_NAME_TO_CODE["washington dc"] = "dc"
US_STATE_NAME_TO_CODE["washington, dc"] = "dc"
US_STATE_NAME_TO_CODE["washington d.c."] = "dc"

US_STATE_ABBREVIATIONS = set(US_STATE_CODE_TO_NAME.keys())
US_STATE_NAMES = set(US_STATE_NAME_TO_CODE.keys())
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
    elif value_norm in ("uk", "gb", "united kingdom", "great britain"):
        val_aliases.update({"uk", "gb", "united kingdom", "great britain"})

    # State aliases expansion (e.g. "tx" <-> "texas", "ca" <-> "california")
    if value_norm in US_STATE_CODE_TO_NAME:
        full_name = normalize_text(US_STATE_CODE_TO_NAME[value_norm])
        val_aliases.add(full_name)
    elif value_norm in US_STATE_NAME_TO_CODE:
        code = US_STATE_NAME_TO_CODE[value_norm]
        val_aliases.add(code)

    # High-priority country aliases matching
    if value_norm in ("us", "usa", "united states", "america", "united states of america"):
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if opt_n in ("united states", "united states of america", "usa", "us"):
                return opt, 1.0
            if opt_n.startswith("united states") or opt_n.startswith("usa"):
                return opt, 0.99
    elif value_norm in ("ca", "can", "canada"):
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if opt_n in ("canada", "ca"):
                return opt, 1.0
            if opt_n.startswith("canada"):
                return opt, 0.99
    elif value_norm in ("uk", "gb", "united kingdom", "great britain"):
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if opt_n in ("united kingdom", "great britain", "uk", "gb"):
                return opt, 1.0
            if opt_n.startswith("united kingdom"):
                return opt, 0.99

    # State exact alias matching
    if value_norm in US_STATE_CODE_TO_NAME or value_norm in US_STATE_NAME_TO_CODE:
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if opt_n in val_aliases:
                return opt, 1.0
            # E.g. "Texas (TX)" or "TX - Texas"
            if any(alias in opt_n for alias in val_aliases if len(alias) > 2):
                return opt, 0.98

    # Timezone alias matching (e.g. "Eastern Daylight Time (Washington D.C)" <-> "Eastern Time (US & Canada)")
    if any(tz in value_norm for tz in ("eastern", "edt", "est")):
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "eastern" in opt_n or opt_n in ("et", "est", "edt"):
                return opt, 1.0
    elif any(tz in value_norm for tz in ("central", "cdt", "cst")):
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "central" in opt_n or opt_n in ("ct", "cst", "cdt"):
                return opt, 1.0
    elif any(tz in value_norm for tz in ("mountain", "mdt", "mst")):
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "mountain" in opt_n or opt_n in ("mt", "mst", "mdt"):
                return opt, 1.0
    elif any(tz in value_norm for tz in ("pacific", "pdt", "pst")):
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "pacific" in opt_n or opt_n in ("pt", "pst", "pdt"):
                return opt, 1.0
    elif "alaska" in value_norm:
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "alaska" in opt_n:
                return opt, 1.0
    elif "hawaii" in value_norm:
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "hawaii" in opt_n:
                return opt, 1.0
    elif "atlantic" in value_norm:
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "atlantic" in opt_n:
                return opt, 1.0

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
            # which prevents short codes ('us', 'ca') from matching inside unrelated words ('australia', 'russia').
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


VISA_SPONSORSHIP_LABEL_KEYWORDS = (
    "visa", "sponsorship", "sponsor", "sponorship", "sponor", "support of", "maintain that authorization", "require assistance"
)
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

# Age Range: always "30-39"
AGE_LABEL_KEYWORDS = ("age range", "your age", "how old are you")
AGE_ANSWER = "30-39"

# Race/Ethnicity: always "I prefer not to answer"
RACE_LABEL_KEYWORDS = ("race", "ethnicity", "identify my ethnicity", "identify your ethnicity")
RACE_ANSWER_KEYWORDS = ("prefer not", "decline", "do not wish")

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
    "employment sponsorship",
    "provide employment sponsorship",
    "require employment sponsorship",
    "conflict of interest",
    "conflicts of interest",
    "potential conflict",
    "close personal relationships",
    "close relative",
    "relative or someone",
    "living in the same household",
    "family member",
    "currently an employee",
    "currently employed by",
    "former employee",
    "previously employed by",
    "previously worked for",
    "previously worked at",
    "have you ever worked for",
    "have you ever worked at",
    "ever worked for",
    "ever worked at",
    "worked at",
    "worked for",
    "require the support of",
    "maintain that authorization",
    "support to maintain",
    "non-compete",
    "non compete",
    "restrictive covenant",
    "disciplinary action",
    "felony",
    "misdemeanor",
    "criminal conviction",
    "ever been convicted",
    "hispanic",
    "latino",
    "are you hispanic",
    "are you latino",
    "are you hispanic/latino",
    "are you hispanic or latino",
    "hispanic/latino",
    "hispanic or latino",
    "hispanic origin",
    "latino origin",
)

# Follow-up questions that ask for explanation only IF the previous question was answered YES
# (e.g. "If you answered yes to the question above, please provide more details." or "If yes, please explain:")
CONDITIONAL_IF_YES_KEYWORDS = (
    "if you answered yes",
    "if you selected yes",
    "if you replied yes",
    "if you said yes",
    "if answered yes",
    "if yes please provide",
    "if yes please explain",
    "if yes please elaborate",
    "if yes please list",
    "if yes please specify",
    "if yes please describe",
    "if yes provide details",
    "if yes explain",
    "if yes specify",
    "if yes list",
    "if yes describe",
    "if applicable please explain",
    "if requiring sponsorship",
    "if you require sponsorship",
    "if sponsorship is required",
    "if you answered yes to any",
    "if yes to any",
)


def resolve_skills_experience_choice(label_norm: str, options_texts: list[str], profile: dict) -> str | None:
    """
    Checks if a choice question asks about specific technical experience/skills
    (e.g. 'Do you have Ruby on Rails Experience?'). If options contain Yes/No,
    checks profile['skills'] and profile['resume_text']. Returns matched option.
    """
    opts_lower = [normalize_text(o) for o in options_texts]
    has_yes = any(o in ("yes", "y", "true") or o.startswith("yes") for o in opts_lower)
    has_no = any(o in ("no", "n", "false") or o.startswith("no") for o in opts_lower)
    if not (has_yes and has_no):
        return None

    if not any(kw in label_norm for kw in ("experience", "familiar", "proficien", "knowledge", "skill", "background in", "worked with")):
        return None

    skills = [normalize_text(s) for s in profile.get("skills", []) if s]
    resume = normalize_text(profile.get("resume_text") or "")
    
    tokens = [t for t in label_norm.split() if len(t) > 2 and t not in (
        "you", "have", "with", "the", "and", "for", "such", "such as", "experience", "frameworks", "tools", "languages", "like", "any", "are"
    )]
    
    match_found = False
    for t in tokens:
        if any(t in s for s in skills) or t in resume:
            match_found = True
            break
            
    target_ans = "yes" if match_found else "no"
    for opt in options_texts:
        if normalize_text(opt) == target_ans or normalize_text(opt).startswith(target_ans):
            return opt
    return None


def resolve_hear_about_us_choice(label_norm: str, options_texts: list[str]) -> str | None:
    """Matches 'How did you hear about us' / source dropdowns to LinkedIn / Job board."""
    if not any(kw in label_norm for kw in ("hear about", "how did you hear", "source", "hear of us")):
        return None
    for opt in options_texts:
        opt_n = normalize_text(opt)
        if any(src in opt_n for src in ("linkedin", "job board", "careers website", "career site", "online", "internet", "website", "company website")):
            return opt
    return None


def resolve_gender_identity_choice(label_norm: str, options_texts: list[str]) -> str | None:
    """Handles detailed gender identity choices (e.g. 'Cisgender - Male', 'Male', 'Prefer not to say')."""
    if "gender identity" in label_norm or "specify your gender" in label_norm:
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "cisgender male" in opt_n or opt_n == "male":
                return opt
        for opt in options_texts:
            opt_n = normalize_text(opt)
            if "prefer not to say" in opt_n or "decline" in opt_n or "prefer not" in opt_n:
                return opt
    return None


def resolve_hispanic_latino_choice(label_norm: str, options_texts: list[str]) -> str | None:
    """Handles Hispanic/Latino/Ethnicity questions across all ATS formats (Yes/No, Not Hispanic, etc.)."""
    if not any(k in label_norm for k in ("hispanic", "latino", "ethnicity")):
        return None

    # 1. Look for explicit "No" / "Not Hispanic" options
    for opt in options_texts:
        opt_n = normalize_text(opt)
        if opt_n in ("no", "n", "false") or opt_n.startswith("no,") or opt_n.startswith("no "):
            return opt
        if "not hispanic" in opt_n or "non hispanic" in opt_n or "non-hispanic" in opt_n:
            return opt

    # 2. Look for "White" if race/ethnicity is combined
    for opt in options_texts:
        opt_n = normalize_text(opt)
        if opt_n in ("white", "white (not hispanic or latino)", "caucasian", "white / caucasian"):
            return opt

    # 3. Fallback to "Decline" / "Prefer not to say" if available
    for opt in options_texts:
        opt_n = normalize_text(opt)
        if any(d in opt_n for d in ("decline", "prefer not", "do not wish", "choose not")):
            return opt

    return None


def resolve_eeo_race_choice(label_norm: str, options_texts: list[str]) -> str | None:
    """Handles Race/EEO self-identification questions (e.g. 'Race', 'Race/Ethnicity')."""
    if not any(k in label_norm for k in ("race", "ethnic origin", "demographic", "eeo")):
        return None
    # Look for White / Caucasian
    for opt in options_texts:
        opt_n = normalize_text(opt)
        if "white" in opt_n or "caucasian" in opt_n:
            return opt
    # Fallback to Decline to Self Identify
    for opt in options_texts:
        opt_n = normalize_text(opt)
        if any(d in opt_n for d in ("decline", "prefer not", "do not wish", "choose not")):
            return opt
    return None


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
    # Use concise resume context (~1200 chars) so local LLMs respond quickly
    resume_text = (profile.get("resume_text") or "")[:1200]
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
        "Keep your answer concise, professional, and specific (2-3 sentences). "
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

Your Resume summary:
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
    try:
        raw_answer = query_ollama(prompt, system_prompt=system_prompt, timeout=config.OLLAMA_LONG_TIMEOUT, options={"num_predict": 256})
    except Exception as e:
        job_logger.warning(f"Ollama call for '{label}' failed or timed out: {e}. Using intelligent fallback.")
        label_lower = label.lower()
        if any(kw in label_lower for kw in ("mission", "inspire", "why do you want", "why join", "interest")):
            raw_answer = f"I am truly inspired by {company_name}'s mission, innovative culture, and technological vision. My background and skills align closely with this role, and I am excited about the opportunity to contribute directly to the team's ongoing success."
        elif any(kw in label_lower for kw in ("experience", "project", "accomplishment", "background")):
            raw_answer = f"Throughout my career, I have developed robust, scalable solutions using modern technologies and best engineering practices. I thrive in collaborative environments and consistently deliver high-impact results."
        elif any(kw in label_lower for kw in ("how did you hear", "hear about", "referral", "source")):
            raw_answer = "I found this role through LinkedIn while researching opportunities in this space."
        else:
            raw_answer = f"I am very interested in this {job_title} role at {company_name} and look forward to contributing my technical expertise and problem-solving abilities to your team."

    cleaned = strip_markdown_formatting(raw_answer).strip()

    # If Ollama returned empty, whitespace, or an incomplete stub, trigger intelligent domain fallback
    if not cleaned or len(cleaned) < 10:
        label_lower = label.lower()
        if any(kw in label_lower for kw in ("mission", "inspire", "why do you want", "why join", "interest")):
            cleaned = f"I am truly inspired by {company_name}'s mission, innovative culture, and technological vision. My background and skills align closely with this role, and I am excited about the opportunity to contribute directly to the team's ongoing success."
        elif any(kw in label_lower for kw in ("experience", "project", "accomplishment", "background")):
            cleaned = f"Throughout my career, I have developed robust, scalable solutions using modern technologies and best engineering practices. I thrive in collaborative environments and consistently deliver high-impact results."
        elif any(kw in label_lower for kw in ("how did you hear", "hear about", "referral", "source")):
            cleaned = "I found this role through LinkedIn while researching opportunities in this space."
        else:
            cleaned = f"I am very interested in this {job_title} role at {company_name} and look forward to contributing my technical expertise and problem-solving abilities to your team."

    job_logger.info(f"--- OLLAMA PROMPT FOR '{label}' ---\nSystem: {system_prompt}\nUser: {prompt}\n----------------------------------")
    job_logger.info(f"--- OLLAMA RESPONSE FOR '{label}' ---\nRaw: {raw_answer}\nCleaned: {cleaned}\n------------------------------------")

    # If the question is about referral / discovery source, never return N/A
    label_lower = label.lower()
    if any(kw in label_lower for kw in ("how did you hear", "hear about", "referral", "source of referral", "how did you find")):
        if "n/a" in cleaned.lower() or len(cleaned) <= 3:
            return "I found this role through LinkedIn while researching opportunities in this space."

    # If the question was asking for a missing URL / link and Ollama replied with N/A, keep it
    if "n/a" in cleaned.lower() and len(cleaned) <= 5:
        is_url = any(kw in label_lower for kw in ("url", "link", "http", "website", "portfolio", "github", "twitter", "blog"))
        if is_url:
            return "N/A"
        return f"I am excited about the {job_title} opportunity at {company_name} and look forward to contributing my technical skills and experience to the team."

    return cleaned


async def ask_ollama_choice(label: str, options: list[str], profile: dict, job_logger, classification: str) -> str:
    """Ollama is given the list of rendered options and must pick one."""
    assert classification == "CHOICE_FIELD", "Ollama choice picker may only run for CHOICE_FIELD"
    profile_context = json_context_string(profile)
    options_str = str(options)

    system_prompt = (
        "You are the candidate applying for this job. The Candidate Profile provided is YOUR personal background and YOUR identity. "
        "Output ONLY the exact text of the option that best matches the question/context based on your identity and background. "
        "NEVER refer to 'the candidate', 'the profile', or yourself as an AI. "
        "CRITICAL INSTRUCTION FOR SALARY: If a question asks whether a target salary range meets your requirements or expectations, "
        "and your profile's expected salary is LESS THAN or WITHIN that range, you MUST select 'Yes'. "
        "If a question asks for a preference or something not explicitly stated, use your professional judgment to deduce a reasonable answer as if you were this person. "
        "Do not include markdown or explanations. Output the exact option text only."
    )
    prompt = f"""
Your Profile:
{profile_context}

Question:
{label}

Options:
{options_str}

Choose the single best matching option. Your response MUST be exactly one of the options from the list above:
"""
    try:
        raw_answer = query_ollama(prompt, system_prompt=system_prompt, timeout=config.OLLAMA_LONG_TIMEOUT, options={"num_predict": 32})
    except Exception as e:
        job_logger.warning(f"Ollama choice call for '{label}' failed or timed out: {e}. Falling back to default option.")
        # Return first non-placeholder option, not blindly options[0]
        for opt in options:
            if normalize_text(opt) not in _SELECT_PLACEHOLDER_TEXTS:
                return opt
        return options[0] if options else ""

    cleaned = strip_markdown_formatting(raw_answer).strip()

    # If Ollama wrapped the answer in quotes or brackets, strip them
    cleaned = re.sub(r"^['\"\[]+|['\"\]]+$", "", cleaned).strip()

    job_logger.info(f"--- OLLAMA PROMPT FOR '{label}' ---\nSystem: {system_prompt}\nUser: {prompt}\n----------------------------------")
    job_logger.info(f"--- OLLAMA RESPONSE FOR '{label}' ---\n{raw_answer}\n------------------------------------")

    # Re-match against actual options to guarantee exact match
    for opt in options:
        if opt.strip().lower() == cleaned.lower():
            return opt.strip()

    # Fuzzy match as fallback
    best, score = best_matching_option(cleaned, options)
    if best and score >= 0.5:
        return best

    # Ultimate fallback: try to find a valid option that isn't a placeholder
    if options:
        for opt in options:
            if normalize_text(opt) not in _SELECT_PLACEHOLDER_TEXTS:
                return opt.strip()
        return options[0]
    return ""

async def ask_ollama_numeric(label: str, profile: dict, job_logger) -> str:
    """Prompt Ollama specifically to output a single numeric integer/decimal."""
    profile_context = json_context_string(profile)
    system_prompt = (
        "You are the candidate applying for this job. The Candidate Profile provided is YOUR personal background and YOUR identity. "
        "The form field requires a purely numeric answer (e.g. number of years, salary number, GPA, percentage). "
        "Output ONLY digits (and a decimal point if applicable). Do NOT include words, currency signs, commas, or explanations. "
        "For example, output '130000' or '8', never '$130,000' or '8 years'."
    )
    prompt = f"""
Your Profile:
{profile_context}

Question:
{label}

Provide the numeric value only:
"""
    try:
        raw_answer = query_ollama(prompt, system_prompt=system_prompt, timeout=config.OLLAMA_LONG_TIMEOUT, options={"num_predict": 16})
    except Exception as e:
        job_logger.warning(f"Ollama numeric call for '{label}' failed: {e}. Using fallback 5.")
        raw_answer = "5"

    cleaned = strip_markdown_formatting(raw_answer).strip()
    digits_only = re.sub(r"[^\d.]", "", cleaned)
    job_logger.info(f"Ollama numeric response for '{label}': raw='{cleaned}' -> extracted='{digits_only}'")
    return digits_only or "0"


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
            // Never treat elements inside the main application form as chat widgets
            if (el.closest('form, #grnhse_app, #ashby_embed, [data-qa*="application-form"], [class*="application-form"], .jobs-container, main')) {
                const isThirdPartyChat = el.closest('[class*="intercom-frame"], [id*="drift-frame"], [class*="zendesk-chat"], [class*="paradox-widget"]');
                if (isThirdPartyChat) return true;
                return false;
            }

            const chatAncestor = el.closest(
                '#intercom-container, [class*="intercom-widget"], [id*="drift-widget"], [class*="drift-widget"], ' +
                '#launcher, .zEwidget, [class*="zendesk-embed"], [class*="paradox-chat"], [id*="paradox-chat"], ' +
                '[aria-label="Live Chat" i], [aria-label="Chat with us" i], [aria-label="Open chat" i], [aria-label="Virtual Assistant" i]'
            );
            if (chatAncestor) return true;

            const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
            const title = (el.getAttribute('title') || '').toLowerCase();

            const chatPrompts = [
                'write a reply...', 'type a message...', 'ask a question...', 'ask anything...',
                'chat with us', 'how can we help?'
            ];
            return chatPrompts.some(p => placeholder === p || title === p);
        }""")
        return bool(is_chat)
    except Exception:
        return False

EXPIRED_JOB_PATTERNS = [
    "page you are looking for no longer exists",
    "job may be no longer available",
    "job is no longer available",
    "this job is no longer available",
    "position is no longer available",
    "this position is no longer available",
    "job posting has expired",
    "this position has been closed",
    "this job has been closed",
    "no longer accepting applications",
    "job not found",
    "site not found",
    "page not found",
    "404 - page not found",
    "404 page not found",
    "410 gone",
    "410 - gone",
    "the requisition has closed",
    "position has been filled",
    "this posting has expired",
    "posting is no longer active",
    "job expired",
    "we could not find the job",
    "this career site is not available",
    "this job requisition is no longer available",
    "no open positions found",
    "session has expired",
    "page has expired"
]

async def detect_expired_or_missing_job(p_page, initial_job_link: str = "") -> tuple[bool, str]:
    """Detects if the page is a 404, closed, or expired job notice, or redirected to general catalog."""
    try:
        title = (await p_page.title()).lower()
        if any(term in title for term in ("404", "not found", "page not found", "gone:", "site not found", "expired")):
            return True, f"Job page title indicates inactive/not found/expired: '{title}'"
    except Exception:
        pass

    try:
        current_url = (p_page.url or "").lower()
        # If the original job link had a specific ID/path, but we got redirected to the general /jobs or /careers listing:
        if initial_job_link:
            init_lower = initial_job_link.lower()
            if any(term in init_lower for term in ("/jobs/", "/job/", "/apply", "/postings/")):
                from urllib.parse import urlparse
                init_p = urlparse(init_lower)
                curr_p = urlparse(current_url)
                if init_p.path.strip("/") != curr_p.path.strip("/"):
                    # Check if the redirected page is the general company jobs portal (ends with /jobs or /careers)
                    if curr_p.path.strip("/").endswith(("jobs", "careers", "openings")):
                        has_search = await p_page.locator("[data-testid*='search'], input[placeholder*='Search' i], input[name*='search' i]").count()
                        if has_search > 0:
                            return True, f"Job URL redirected to general job board ({current_url}) - position has expired or is no longer available"
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
# Combobox / Autocomplete handling
#
# Custom autocomplete widgets (React-Select, Downshift, Material-UI Autocomplete,
# Vuetify v-autocomplete, Ashby/Workday/Greenhouse custom comboboxes) do not use
# native <select> elements. Instead they use an <input role="combobox"> paired
# with a dynamically rendered [role="listbox"] or floating dropdown list.
# ---------------------------------------------------------------------------

async def is_combobox_element(elem: ElementHandle) -> bool:
    """Returns True if the element acts as an autocomplete / custom combobox input."""
    try:
        role = (await elem.get_attribute("role") or "").lower()
        if role == "combobox":
            return True
        aria_autocomplete = (await elem.get_attribute("aria-autocomplete") or "").lower()
        if aria_autocomplete in ("list", "both"):
            return True
        has_popup = (await elem.get_attribute("aria-haspopup") or "").lower()
        if has_popup in ("listbox", "true", "menu"):
            return True
        class_name = (await elem.get_attribute("class") or "").lower()
        if any(term in class_name for term in ["combobox", "autocomplete", "typeahead", "react-select"]):
            return True
    except Exception:
        pass
    return False


async def find_visible_listbox_options(frame, wait_ms: int = LISTBOX_WAIT_MS, elem: ElementHandle | None = None) -> tuple[list[str], str | None, object]:
    """
    Polls for open listbox options across both the active frame and the top-level
    page using high-speed in-page DOM evaluation.
    """
    contexts = [frame]
    if hasattr(frame, "page") and frame.page != frame:
        contexts.append(frame.page)

    deadline = asyncio.get_event_loop().time() + (wait_ms / 1000)

    while True:
        for context in contexts:
            try:
                res = await asyncio.wait_for(context.evaluate("""() => {
                    // Standard flat selectors
                    const selectors = [
                        "[role='option']",
                        "[role='listbox'] li",
                        "[role='listbox'] div",
                        "ul[class*='select'] li",
                        "ul[class*='dropdown'] li",
                        "ul[class*='menu'] li",
                        "div[class*='option']",
                        "div[class*='Option']",
                        "div[class*='menu'] [class*='option']",
                        "div[class*='select__option']",
                        "div[class*='react-select__option']",
                        "div[class*='-option']",
                        "[id*='react-select'][id*='-option']",
                        ".ashby-menu-item",
                        "[data-automation-id='promptOption']",
                        ".select2-results__option"
                    ];
                    for (const sel of selectors) {
                        try {
                            const els = document.querySelectorAll(sel);
                            const texts = [];
                            for (const el of els) {
                                if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                                    if (el.closest('nav, header, [role="navigation"], .sticky-nav, [class*="header"]')) continue;
                                    const txt = (el.innerText || el.textContent || '').trim();
                                    if (txt) texts.push(txt);
                                }
                            }
                            if (texts.length > 0) return { selector: sel, texts: texts };
                        } catch(e) {}
                    }
                    // Shadow DOM piercing for SmartRecruiters (spl-dropdown-item) and other custom elements
                    function deepQueryAll(root, selList) {
                        let results = [];
                        function visit(r) {
                            for (const sel of selList) {
                                try {
                                    const found = Array.from(r.querySelectorAll(sel)).filter(
                                        el => el.offsetWidth > 0 && el.offsetHeight > 0
                                    );
                                    results = results.concat(found);
                                } catch(e) {}
                            }
                            const children = r.querySelectorAll ? Array.from(r.querySelectorAll('*')) : [];
                            for (const child of children) {
                                if (child.shadowRoot) visit(child.shadowRoot);
                            }
                        }
                        visit(root);
                        return results;
                    }
                    const shadowSelectors = [
                        "spl-dropdown-item",
                        "[role='option']",
                        "li[class*='item']",
                        "li[class*='option']",
                        "li"
                    ];
                    const shadowEls = deepQueryAll(document, shadowSelectors);
                    const shadowTexts = [];
                    for (const el of shadowEls) {
                        if (el.closest && el.closest('nav, header, [role="navigation"], .sticky-nav, [class*="header"]')) continue;
                        const txt = (el.innerText || el.textContent || '').trim();
                        if (txt && txt.length > 0 && txt.length < 200) shadowTexts.push(txt);
                    }
                    if (shadowTexts.length > 0) return { selector: '__shadow__', texts: [...new Set(shadowTexts)] };
                    return null;
                }"""), timeout=0.8)
                if res and res.get("texts"):
                    return res["texts"], res["selector"], context
            except Exception:
                pass

        if asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(0.1)

    return [], None, None


async def click_option_by_text(context, selector: str, text: str) -> bool:
    """Clicks a listbox option matching text with high-speed JS evaluation and shadow DOM fallback."""
    clean_js = "str => str.toLowerCase().replace(/[^a-z0-9]/g, ' ').replace(/\\s+/g, ' ').trim()"
    try:
        res = await asyncio.wait_for(context.evaluate("""({ selector, text }) => {
            const clean = str => str.toLowerCase().replace(/[^a-z0-9]/g, ' ').replace(/\\s+/g, ' ').trim();
            const target = clean(text);

            // Helper: get all matching elements from flat DOM and shadow DOM
            function getAllElements(root, sel) {
                let results = [];
                if (sel === '__shadow__') {
                    // Shadow DOM search: collect spl-dropdown-item, [role=option], li
                    const shadowSels = ['spl-dropdown-item', "[role='option']", 'li[class*=\'item\']', 'li[class*=\'option\']', 'li'];
                    function visit(r) {
                        for (const s of shadowSels) {
                            try { results = results.concat(Array.from(r.querySelectorAll(s))); } catch(e) {}
                        }
                        try {
                            for (const child of Array.from(r.querySelectorAll('*'))) {
                                if (child.shadowRoot) visit(child.shadowRoot);
                            }
                        } catch(e) {}
                    }
                    visit(root);
                } else {
                    try { results = Array.from(root.querySelectorAll(sel)); } catch(e) {}
                }
                return results.filter(el => el.offsetWidth > 0 && el.offsetHeight > 0);
            }

            function tryClick(els) {
                // 1. Exact match
                for (const el of els) {
                    const cur = clean(el.innerText || el.textContent || '');
                    if (cur === target) { el.scrollIntoView({ block: 'nearest' }); el.click(); return true; }
                }
                // 2. Starts with
                for (const el of els) {
                    const cur = clean(el.innerText || el.textContent || '');
                    if (cur.startsWith(target) || target.startsWith(cur)) { el.scrollIntoView({ block: 'nearest' }); el.click(); return true; }
                }
                // 3. Substring
                for (const el of els) {
                    const cur = clean(el.innerText || el.textContent || '');
                    if (cur.includes(target) || target.includes(cur)) { el.scrollIntoView({ block: 'nearest' }); el.click(); return true; }
                }
                return false;
            }

            const els = getAllElements(document, selector);
            return tryClick(els);
        }""", {"selector": selector, "text": text}), timeout=1.5)
        if res:
            return True
    except Exception:
        pass
    try:
        if selector != '__shadow__':
            loc = context.locator(selector).filter(has_text=text).first
            if await loc.is_visible():
                await loc.click(timeout=1500)
                return True
    except Exception:
        pass
    return False
    return False


async def read_element_value(elem: ElementHandle) -> str:
    """Best-effort read of a field's current committed value, for post-selection verification and skip checks."""
    try:
        val = await elem.evaluate("""el => {
            if (el.tagName === 'SELECT') {
                return (el.value || '').trim();
            }
            // First check if this element is inside a React-Select / custom combobox container
            const container = el.closest('.select__control, .select-shell, .select__container, [class*="control"], [class*="container"], [class*="select"]');
            if (container) {
                const singleValue = container.querySelector('.select__single-value, [class*="singleValue"], [class*="single-value"], [class*="SingleValue"], [class*="MultiValue"], .select2-selection__rendered');
                if (singleValue && singleValue.textContent && singleValue.textContent.trim().length > 0 && singleValue.textContent.trim() !== 'Please select') {
                    return singleValue.textContent.trim();
                }
                // If container is a custom combobox/select, the input itself is just a search filter input (e.g. .select__input),
                // so do NOT return el.value as a committed form value if no singleValue is selected!
                if (el.tagName !== 'SELECT' && (el.classList.contains('select__input') || el.closest('.select__input-container') || el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete'))) {
                    const hiddenInput = container.querySelector('input[type="hidden"]');
                    if (hiddenInput && hiddenInput.value && hiddenInput.value.trim().length > 0) {
                        return hiddenInput.value.trim();
                    }
                    return "";
                }
            }
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                return (el.value || '').trim();
            }
            if (el.isContentEditable) {
                return (el.textContent || '').trim();
            }
            // Deep shadow DOM piercing for custom web components (e.g. SmartRecruiters spl-input)
            function deepFindInput(root) {
                if (!root) return null;
                const inp = root.querySelector('input, textarea');
                if (inp) return inp;
                const all = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                for (const ch of all) {
                    if (ch.shadowRoot) {
                        const res = deepFindInput(ch.shadowRoot);
                        if (res) return res;
                    }
                }
                return null;
            }
            const inner = deepFindInput(el.shadowRoot || el);
            if (inner) return (inner.value || '').trim();
            return (el.value || '').trim();
        }""")
        return (val or "").strip()
    except Exception:
        return ""


async def handle_combobox_field(frame, elem: ElementHandle, label: str, profile: dict, job_logger, matched_key: str | None, field_attempts: dict = None) -> bool:
    """
    Universal autocomplete/combobox strategy for any field detected as a
    combobox.
    """
    # Check if element is still attached to DOM; reacquire if needed (e.g. after resume upload re-render)
    try:
        is_attached = await elem.evaluate("el => el.isConnected === true")
        if not is_attached and frame and label:
            fresh = await reacquire_field_element(frame, label)
            if fresh:
                elem = fresh
    except Exception:
        if frame and label:
            fresh = await reacquire_field_element(frame, label)
            if fresh:
                elem = fresh

    # Just like text fields, don't re-fill a combobox if it already has a value
    # during validation recovery or multi-step loops where the page didn't advance.
    existing_value = await read_element_value(elem)
    if existing_value and len(existing_value) > 1:
        return True

    if field_attempts is not None and label:
        lbl_lower = label.lower()
        field_attempts[lbl_lower] = field_attempts.get(lbl_lower, 0) + 1
        if field_attempts[lbl_lower] > 3:
            job_logger.warning(f"Skipping combobox '{label}' - exceeded max fill attempts (3).")
            return False

    # Skip conditional follow-up comboboxes introduced by "If yes, ..." phrasing.
    # These only apply when a preceding question was answered "Yes" — e.g.
    # "If yes, please select your most recent employment type" which appears after
    # "Have you ever worked at AbbVie?". Since the candidate answers "No" to that
    # question, the follow-up dropdown must be left blank to avoid validation errors.
    label_lower_skip = label.lower()
    if label_lower_skip.startswith("if yes") or label_lower_skip.startswith("if so"):
        job_logger.info(f"Skipping conditional follow-up combobox: '{label}' (precondition was answered No)")
        log_field_decision(job_logger, label, "CHOICE_FIELD", "skipped-conditional-if-yes", None)
        return True

    value = get_profile_value(profile, matched_key) if matched_key else None
    if not value and (matched_key in ("country_code", "phone_country_code") or any(kw in label.lower() for kw in ("country code", "phone code", "dialing code"))):
        value = "United States"
    is_location = matched_key in ("location", "city") or any(kw in label.lower() for kw in ("location", "city", "where are you based", "currently based", "residence")) or ("where are you" in label.lower() and "based" in label.lower())
    # Fallback: if we know this is a location field but classify_field didn't match
    # a profile key, pull the location value from the profile directly.
    if is_location and not value:
        value = profile.get("location") or profile.get("city") or ""
        
    # Provide a typeable "No" for Hispanic/Latino comboboxes that require typing to open
    if not value and matched_key is None and any(k in label.lower() for k in ("hispanic", "latino", "ethnicity")):
        value = "No"
    if not value and matched_key is None and any(k in label.lower() for k in ("gender", "sex")):
        value = "Male"
    if not value and matched_key is None and "race" in label.lower():
        value = "prefer not"
    if not value and matched_key is None and "veteran" in label.lower():
        value = "not a protected veteran"

    try:
        try:
            await elem.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        # For custom comboboxes / React-Select, click the control container if present to open the menu
        try:
            container = await elem.evaluate_handle("el => el.closest('.select__control, .select-shell, .select__container, [class*=\"control\"], [class*=\"select\"]')")
            container_elem = container.as_element()
            if container_elem:
                await container_elem.click(timeout=2000)
            else:
                await elem.click(timeout=2000)
        except Exception:
            try:
                await elem.click(force=True, timeout=2000)
            except Exception:
                pass

        type_value = value
        if value and is_location:
            # Type only the city name (e.g. "Haltom City") 
            # instead of the full location string (e.g. "Haltom City, TX") to help 
            # the dropdown appear correctly.
            type_value = value.split(',')[0].strip()
        elif value and (matched_key == "state" or "state" in label.lower()):
            type_value = US_STATE_CODE_TO_NAME.get(str(value).strip().lower(), str(value).strip())
        elif value and (matched_key == "city" or "city" in label.lower()):
            type_value = str(value).strip()
        elif value and (matched_key == "country" or "country" in label.lower()):
            if str(value).upper() in ("US", "USA", "UNITED STATES"):
                type_value = "United States"
            elif str(value).upper() in ("CA", "CAN", "CANADA"):
                type_value = "Canada"
            elif str(value).upper() in ("UK", "GB", "UNITED KINGDOM"):
                type_value = "United Kingdom"
            else:
                type_value = str(value).strip()
        elif value and matched_key == "education_degree":
            # Type just the first prefix (e.g. "Bachelor") so it triggers dropdowns that might strictly expect "Bachelor's" with an apostrophe
            type_value = value.split("'")[0].split("’")[0].split()[0].strip()
        elif value and (matched_key == "timezone" or "time zone" in label.lower() or "timezone" in label.lower()):
            val_lower = str(value).lower()
            if any(k in val_lower for k in ("eastern", "edt", "est")):
                type_value = "Eastern"
            elif any(k in val_lower for k in ("central", "cdt", "cst")):
                type_value = "Central"
            elif any(k in val_lower for k in ("mountain", "mdt", "mst")):
                type_value = "Mountain"
            elif any(k in val_lower for k in ("pacific", "pdt", "pst")):
                type_value = "Pacific"
            elif "alaska" in val_lower:
                type_value = "Alaska"
            elif "hawaii" in val_lower:
                type_value = "Hawaii"
            elif "atlantic" in val_lower:
                type_value = "Atlantic"
            else:
                type_value = None
        elif matched_key in ("visa_sponsorship_needed", "work_authorization"):
            # Do NOT type profile strings like "no" / "yes" into search box before opening dropdown
            type_value = None

        if value and is_location:
            committed = await read_element_value(elem)
            if committed:
                ev_norm = normalize_text(committed)
                val_norm = normalize_text(value)
                type_val_norm = normalize_text(type_value or "")
                if (type_val_norm and type_val_norm in ev_norm) or (val_norm and val_norm in ev_norm) or (ev_norm and ev_norm in val_norm):
                    job_logger.info(f"Location '{label}' already populated with '{committed}' matching profile — skipping re-fill.")
                    log_field_decision(job_logger, label, "PROFILE_FIELD", "profile.json (pre-filled)", committed)
                    return True

        target_input = elem
        try:
            inner = await elem.evaluate_handle("""el => {
                function deepFindInput(root) {
                    if (!root) return null;
                    const inp = root.querySelector('input, textarea');
                    if (inp) return inp;
                    const children = root.querySelectorAll('*');
                    for (const ch of children) {
                        if (ch.shadowRoot) {
                            const res = deepFindInput(ch.shadowRoot);
                            if (res) return res;
                        }
                    }
                    return null;
                }
                const found = deepFindInput(el.shadowRoot || el);
                return found || el;
            }""")
            if inner and inner.as_element():
                target_input = inner.as_element()
        except Exception:
            pass

        if type_value:
            # Clear any pre-existing content before typing
            try:
                await target_input.click(timeout=1000)
                await target_input.press("Control+a", timeout=1000)
                await target_input.press("Backspace", timeout=1000)
            except Exception:
                try:
                    await target_input.evaluate("el => { if ('value' in el) el.value = ''; }")
                except Exception:
                    pass
            
            try:
                await target_input.focus(timeout=1000)
            except Exception:
                pass

            try:
                await target_input.fill(str(type_value), timeout=2000)
            except Exception:
                try:
                    await target_input.evaluate("""(el, v) => {
                        el.value = v;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", str(type_value))
                except Exception:
                    pass
            
            # Wait a bit extra after typing for the network request / dropdown to render
            await asyncio.sleep(0.4)
        else:
            # No type_value — open the dropdown by clicking and then sending ArrowDown.
            # This is critical for EEO/compliance comboboxes (Gender, Race, Protected Veteran,
            # Disability) that have no profile value to type, and also for SmartRecruiters'
            # spl-input components where clicking alone does not render the options list.
            try:
                await target_input.click(timeout=1000)
                await asyncio.sleep(0.2)
                await target_input.press("ArrowDown", timeout=1000)
                await asyncio.sleep(0.4)
            except Exception:
                try:
                    await elem.click(force=True, timeout=1000)
                    await asyncio.sleep(0.2)
                    await elem.press("ArrowDown", timeout=1000)
                    await asyncio.sleep(0.4)
                except Exception:
                    pass

        option_texts, option_selector, target_context = await find_visible_listbox_options(frame, wait_ms=1500, elem=elem)
        
        # If typing filtered out all options or menu hasn't opened yet:
        if not option_texts:
            try:
                # Clear the search box to restore full options list and nudge with ArrowDown
                await elem.evaluate("el => { if ('value' in el) el.value = ''; }")
                await elem.press("ArrowDown", timeout=1000)
                await asyncio.sleep(0.5)
                option_texts, option_selector, target_context = await find_visible_listbox_options(frame, wait_ms=1500, elem=elem)
            except Exception:
                pass

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
            elif any(kw in label_norm for kw in AGE_LABEL_KEYWORDS):
                for opt in option_texts:
                    if AGE_ANSWER in normalize_text(opt):
                        best_text = opt
                        score = 1.0
                        source = f"hardcoded-rule (age: {AGE_ANSWER})"
                        break
            elif any(kw in label_norm for kw in RACE_LABEL_KEYWORDS):
                for ans_kw in RACE_ANSWER_KEYWORDS:
                    for opt in option_texts:
                        if ans_kw in normalize_text(opt):
                            best_text = opt
                            score = 1.0
                            source = "hardcoded-rule (race: decline)"
                            break
                    if best_text:
                        break
            elif "pronoun" in label_norm:
                for preferred in PRONOUNS_PREFERRED:
                    for opt in option_texts:
                        if normalize_text(opt) in [normalize_text(preferred), preferred.replace("/", " ")]:
                            best_text = opt
                            score = 1.0
                            source = "hardcoded-rule (pronouns: He/Him)"
                            break
                    if best_text:
                        break
                if not best_text:
                    for fallback in PRONOUNS_FALLBACK:
                        for opt in option_texts:
                            if normalize_text(fallback) in normalize_text(opt):
                                best_text = opt
                                score = 1.0
                                source = "hardcoded-rule (pronouns: Use name only fallback)"
                                break
                        if best_text:
                            break
            elif any(kw in label_norm for kw in ALWAYS_YES_LABEL_KEYWORDS):
                for opt in option_texts:
                    if normalize_text(opt) in ("yes", "y", "true") or normalize_text(opt).startswith("yes"):
                        best_text = opt
                        score = 1.0
                        source = "hardcoded-rule (always yes)"
                        break
            elif any(kw in label_norm for kw in ALWAYS_NO_LABEL_KEYWORDS):
                for opt in option_texts:
                    if normalize_text(opt) in ("no", "n", "false") or normalize_text(opt).startswith("no"):
                        best_text = opt
                        score = 1.0
                        source = "hardcoded-rule (always no)"
                        break

            if not best_text:
                forced_choice = resolve_visa_sponsorship_choice(label_norm, option_texts, profile)
                if forced_choice:
                    best_text = forced_choice
                    score = 1.0
                    source = "profile.json (visa sponsorship not needed -> legally authorized statement)"
                elif matched_key == "visa_sponsorship_needed":
                    needs_sponsorship = interpret_yes_no(profile.get("visa_sponsorship_needed"))
                    target_ans = "no" if needs_sponsorship is False else "yes"
                    for opt in option_texts:
                        if normalize_text(opt) == target_ans or normalize_text(opt).startswith(target_ans):
                            best_text = opt
                            score = 1.0
                            source = f"profile.json (visa_sponsorship_needed={needs_sponsorship})"
                            break
                elif matched_key == "work_authorization":
                    is_auth = interpret_yes_no(profile.get("work_authorization"))
                    target_ans = "yes" if is_auth is not False else "no"
                    for opt in option_texts:
                        if normalize_text(opt) == target_ans or normalize_text(opt).startswith(target_ans):
                            best_text = opt
                            score = 1.0
                            source = f"profile.json (work_authorization={is_auth})"
                            break
                elif resolve_gender_identity_choice(label_norm, option_texts):
                    best_text = resolve_gender_identity_choice(label_norm, option_texts)
                    score = 1.0
                    source = "hardcoded-rule (gender identity)"
                elif resolve_hispanic_latino_choice(label_norm, option_texts):
                    best_text = resolve_hispanic_latino_choice(label_norm, option_texts)
                    score = 1.0
                    source = "hardcoded-rule (hispanic/latino: no)"
                elif resolve_eeo_race_choice(label_norm, option_texts):
                    best_text = resolve_eeo_race_choice(label_norm, option_texts)
                    score = 1.0
                    source = "hardcoded-rule (race: white/decline)"
                elif resolve_hear_about_us_choice(label_norm, option_texts):
                    best_text = resolve_hear_about_us_choice(label_norm, option_texts)
                    score = 1.0
                    source = "hardcoded-rule (hear about us: job board/linkedin)"
                elif resolve_skills_experience_choice(label_norm, option_texts, profile):
                    best_text = resolve_skills_experience_choice(label_norm, option_texts, profile)
                    score = 1.0
                    source = "profile.json (skills/resume match)"
                elif value:
                    best_text, score = best_matching_option(value, option_texts)
                    source = "dropdown match"

                if score < MATCH_THRESHOLD and is_location and option_texts:
                    first_opt_norm = normalize_text(option_texts[0])
                    val_tokens = [t for t in normalize_text(value).split() if len(t) > 2]
                    if any(t in first_opt_norm for t in val_tokens):
                        best_text = option_texts[0]
                        score = 1.0
                        source = "location-first-dropdown-result"
                    else:
                        best_text = None
                        score = 0.0

            if not best_text or score < MATCH_THRESHOLD:
                choice = await ask_ollama_choice(label, option_texts, profile, job_logger, "CHOICE_FIELD")
                normalized_best, _ = best_matching_option(choice, option_texts)
                best_text = normalized_best or choice
                score = 1.0
                source = "ollama"

            if best_text and score >= MATCH_THRESHOLD:
                clicked = await click_option_by_text(target_context, option_selector, best_text)
                if clicked:
                    await asyncio.sleep(0.3)
                    committed = await read_element_value(elem)
                    if committed and best_text.lower() not in committed.lower() and committed.lower() not in best_text.lower():
                        job_logger.warning(f"Combobox '{label}': committed value '{committed}' doesn't clearly match selected option '{best_text}'.")
                    log_field_decision(job_logger, label, "PROFILE_FIELD" if value else "CHOICE_FIELD", source, best_text)
                    return True

        # --- No dropdown appeared on initial load (or click didn't land) ---
        if value and is_location:
            # Step 4: Keyboard nudge & blind commit
            try:
                await elem.press("ArrowDown", timeout=1000)
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
                await elem.press("Enter", timeout=1000)
                await asyncio.sleep(0.3)
                committed = await read_element_value(elem)
                if committed and len(committed) > 3 and committed.lower() != type_value.lower():
                    job_logger.info(f"Location '{label}': blindly committed '{committed}' via ArrowDown+Enter.")
                    log_field_decision(job_logger, label, "PROFILE_FIELD", "location-keyboard-commit", committed)
                    return True
            except Exception:
                pass

            # Step 5: Dropdown never appeared and keyboard commit failed — field accepts plain text.
            committed = await read_element_value(target_input)
            if not committed:
                try:
                    await target_input.click(timeout=1000)
                    await target_input.press("Control+a", timeout=1000)
                    await target_input.press("Backspace", timeout=1000)
                except Exception:
                    pass
                try:
                    await target_input.type(value, delay=20, timeout=2000)
                except Exception:
                    await target_input.fill(value, timeout=2000)
                await asyncio.sleep(0.2)
                try:
                    await target_input.press("Tab", timeout=1000)
                except Exception:
                    pass

            job_logger.info(f"Location '{label}': no autocomplete dropdown appeared — keeping typed value '{committed or value}' as plain text.")
            log_field_decision(job_logger, label, "PROFILE_FIELD", "profile.json (plain-text fallback)", committed or value)
            return True

        if value:
            # Check if this element is a standard text input vs a strict custom dropdown
            is_custom_select = False
            try:
                is_custom_select = await elem.evaluate("""el => {
                    return !!el.closest('.select__control, .select-shell, .select__container, [class*="control"], [class*="select"], [role="combobox"]');
                }""")
            except Exception:
                pass

            if not is_custom_select:
                # Non-location standard input with datalist / autocomplete hints - plain text input
                log_field_decision(job_logger, label, "PROFILE_FIELD", "profile.json", value)
                return True
            else:
                job_logger.warning(f"Custom combobox '{label}' could not match or click an option for value '{value}'. Leaving for retry.")
                return False

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
        # 1. Group-specific logic (for radio groups, yes/no buttons, custom web components)
        if is_group:
            group_label = await element.evaluate("""el => {
                // Check shadowRoot for fieldset > legend or direct legend
                if (el.shadowRoot) {
                    let leg = el.shadowRoot.querySelector('legend, [role="heading"], .spl-radio-group__legend, .legend');
                    if (leg && leg.innerText && leg.innerText.trim()) return leg.innerText.trim();
                }
                // Check direct children or light DOM slotted legend / label
                let internalLeg = el.querySelector('legend, [slot="legend"], [class*="legend"], [class*="question"], [class*="title"], h3, h4, h5, p');
                if (internalLeg && internalLeg.innerText && internalLeg.innerText.trim()) {
                    let t = internalLeg.innerText.trim();
                    if (t.length > 5) return t;
                }
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

        # 1.5. Check direct attributes on the element
        direct_aria = await element.get_attribute("aria-label")
        if direct_aria and direct_aria.strip():
            return direct_aria.strip()

        # 2. Check explicit label associations
        elem_id = await element.get_attribute("id")
        if elem_id:
            label_elem = await frame.query_selector(f"label[for='{elem_id}']")
            if label_elem:
                label_text = await label_elem.inner_text()
                if label_text.strip():
                    return label_text.strip()
            # Also try stripping '-input' or similar suffixes
            if elem_id.endswith('-input'):
                label_elem2 = await frame.query_selector(f"label[for='{elem_id[:-6]}']")
                if label_elem2:
                    label_text2 = await label_elem2.inner_text()
                    if label_text2.strip():
                        return label_text2.strip()

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
            if label_text.strip() and len(re.sub(r'[*:\s]', '', label_text.strip())) > 0:
                return label_text.strip()

        # 3. Check custom element host or enclosing form element container (e.g. spl-checkbox, spl-input, spl-autocomplete, spl-form-element, spl-radio-group)
        host_label = await element.evaluate("""el => {
            let node = el;
            let hosts = [];
            while (node) {
                if (node.tagName && (node.tagName.toLowerCase().startsWith('spl-') || node.className && typeof node.className === 'string' && (node.className.includes('form-group') || node.className.includes('field-wrapper')))) {
                    if (!node.tagName.toLowerCase().includes('internal')) {
                        hosts.push(node);
                    }
                }
                if (node.getRootNode && node.getRootNode().host) {
                    node = node.getRootNode().host;
                } else {
                    node = node.parentElement;
                }
            }
            
            // Search hosts from bottom-up (closest to element first)
            for (let h of hosts) {
                let lbl = h.getAttribute('label') || h.getAttribute('aria-label') || '';
                if (lbl && lbl.trim().length > 1) {
                    let cleaned = lbl.trim().replace(/^Select /i, '').trim(); // SmartRecruiters often prefixes aria-labels with 'Select '
                    return cleaned;
                }
                
                let slotLbl = h.querySelector('[slot="label-content"]');
                if (slotLbl && slotLbl.innerText && slotLbl.innerText.trim()) {
                    let t = slotLbl.innerText.trim();
                    if (t.replace(/[*:\\s]/g, '').length > 0) return t;
                }
                
                let realLabel = h.querySelector('label');
                if (realLabel && realLabel.innerText && realLabel.innerText.trim()) {
                    let t = realLabel.innerText.trim();
                    if (t.replace(/[*:\\s]/g, '').length > 0) return t;
                }
            }
            
            // If no label found in hosts, check previous siblings of hosts
            for (let h of hosts) {
                let prev = h.previousElementSibling;
                while (prev) {
                    let t = prev.innerText || prev.textContent || '';
                    if (t.trim() && t.trim().length > 5) return t.trim();
                    prev = prev.previousElementSibling;
                }
            }
            
            // Fallback to closest non-empty text content of the hosts
            for (let h of hosts) {
                let txt = h.innerText || h.textContent || '';
                let lines = txt.split('\\n').map(l => l.trim());
                for (let line of lines) {
                    if (line.replace(/[*:\\s]/g, '').length > 0) return line;
                }
            }
            return '';
        }""")
        if host_label and host_label.strip() and len(re.sub(r'[*:\s]', '', host_label.strip())) > 0:
            return host_label.strip()

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
                const lines = parent.innerText.split('\\n').map(l => l.trim()).filter(l => l.replace(/[*:\\s]/g, '').length > 0);
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
    if label:
        clean = re.sub(r'[*:\s]', '', label)
        if len(clean) == 0:
            return ""
    if label and len(label) > 200:
        return label[:197] + "..."
    return label or ""


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


async def reacquire_field_element(frame, label: str) -> ElementHandle | None:
    """Finds a fresh handle for an element matching a label if the original handle became detached."""
    if not frame or not label:
        return None
    try:
        clean_lbl = label.strip()
        # 1. Search by label text association
        label_elems = await frame.query_selector_all("label")
        for lbl in label_elems:
            try:
                t = (await lbl.inner_text()).strip()
                if (clean_lbl.lower() in t.lower()) or (t.lower() in clean_lbl.lower() and len(t) > 5):
                    for_id = await lbl.get_attribute("for")
                    if for_id:
                        target = await frame.query_selector(f"[id='{for_id}']")
                        if target and await target.is_visible():
                            return target
                    target = await lbl.query_selector("input, textarea, select, [contenteditable='true']")
                    if target and await target.is_visible():
                        return target
                    cand = await lbl.evaluate_handle("el => { let s = el.nextElementSibling; return s ? (s.querySelector('input, textarea, select') || s) : null; }")
                    if cand and cand.as_element():
                        return cand.as_element()
            except Exception:
                continue

        # 2. Search by placeholder / aria-label / name
        first_few_words = clean_lbl.split()[:4]
        search_snippet = " ".join(first_few_words) if first_few_words else clean_lbl[:20]
        for attr in ("placeholder", "aria-label", "name"):
            cand = await frame.query_selector(f"[{attr}*='{search_snippet}']")
            if cand and await cand.is_visible():
                return cand
    except Exception:
        pass
    return None


async def set_field_value(elem: ElementHandle, value: str, frame=None, label: str = None) -> None:
    """Sets a value on a native input/textarea/select, or types into a contenteditable element with auto-reacquisition."""
    target_elem = elem
    try:
        # Deep pierce through shadow DOMs to find the actual input element
        inner = await target_elem.evaluate_handle("""el => {
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el;
            
            function deepFindInput(root) {
                if (!root) return null;
                const inp = root.querySelector('input, textarea');
                if (inp) return inp;
                const children = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                for (const ch of children) {
                    if (ch.shadowRoot) {
                        const res = deepFindInput(ch.shadowRoot);
                        if (res) return res;
                    }
                }
                return null;
            }
            
            const found = deepFindInput(el.shadowRoot || el);
            return found || el;
        }""")
        if inner and inner.as_element():
            target_elem = inner.as_element()
    except Exception:
        pass

    try:
        is_attached = await target_elem.evaluate("el => el.isConnected === true")
        if not is_attached and frame and label:
            fresh = await reacquire_field_element(frame, label)
            if fresh:
                target_elem = fresh
    except Exception:
        if frame and label:
            fresh = await reacquire_field_element(frame, label)
            if fresh:
                target_elem = fresh

    is_editable = False
    try:
        is_editable = await target_elem.evaluate("el => el.isContentEditable === true")
    except Exception:
        pass

    try:
        input_type = (await target_elem.get_attribute("type") or "").lower()
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
        await target_elem.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    try:
        await target_elem.click(timeout=2000)
    except Exception:
        try:
            await target_elem.click(force=True, timeout=2000)
        except Exception:
            pass

    try:
        await target_elem.press("Control+a", timeout=1000)
        await target_elem.press("Backspace", timeout=1000)
    except Exception:
        pass

    try:
        # Fast, bulk fill to avoid unnatural typing delays and timeouts
        if is_editable:
            await target_elem.evaluate("(el, v) => { el.innerText = v; }", str(value))
        else:
            await target_elem.fill(str(value), timeout=2500)
            # Verify if fill worked. Some fields (like "Confirm Email") block pasting/fill.
            current_val = await target_elem.evaluate("el => el.value")
            if not current_val:
                await target_elem.type(str(value), delay=50, timeout=5000)
    except Exception:
        try:
            # Fallback to direct JS property set + events
            await target_elem.evaluate("""(el, v) => {
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }""", str(value))
            return
        except Exception:
            pass

    try:
        await target_elem.evaluate("""el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }""")
        try:
            await target_elem.press("Tab", timeout=1000)
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Field fillers
# ---------------------------------------------------------------------------

async def fill_text_field(frame, elem: ElementHandle, label: str, profile: dict, job_logger, field_attempts: dict = None) -> bool:
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
            elif "email" in label.lower() and profile.get("email") and existing_value.strip().lower() != str(profile.get("email")).strip().lower():
                pass  # Pre-filled email from resume autofill differs from target profile email; overwrite
            else:
                return True
        except Exception:
            return True

    if field_attempts is not None and label:
        lbl_lower = label.lower()
        field_attempts[lbl_lower] = field_attempts.get(lbl_lower, 0) + 1
        if field_attempts[lbl_lower] > 3:
            job_logger.warning(f"Skipping text field '{label}' - exceeded max fill attempts (3).")
            return False

    field_type = "combobox" if await is_combobox_element(elem) else "text"
    classification, matched_key = classify_field(label, field_type, profile)

    if classification == "CHOICE_FIELD":
        return await handle_combobox_field(frame, elem, label, profile, job_logger, matched_key)

    label_norm = normalize_text(label)

    # --- Permanent hardcoded rules for text fields (run before profile matching) ---
    if any(kw in label_norm for kw in ALWAYS_YES_LABEL_KEYWORDS):
        try:
            await set_field_value(elem, "Yes", frame=frame, label=label)
            log_field_decision(job_logger, label, classification, "hardcoded-rule (always yes text)", "Yes")
            return True
        except Exception as e:
            job_logger.error(f"Failed to fill text field '{label}': {e}")
            return False

    if any(kw in label_norm for kw in ALWAYS_NO_LABEL_KEYWORDS):
        try:
            await set_field_value(elem, "No", frame=frame, label=label)
            log_field_decision(job_logger, label, classification, "hardcoded-rule (always no text)", "No")
            return True
        except Exception as e:
            job_logger.error(f"Failed to fill text field '{label}': {e}")
            return False

    # Conditional follow-up fields for 'Yes' responses (e.g. "If you answered yes to the question above, please provide more details.")
    # Since our profile answers 'No' to sponsorship, relatives, conflicts of interest, etc.,
    # skip optional follow-up fields or fill 'N/A' if required.
    if any(kw in label_norm for kw in CONDITIONAL_IF_YES_KEYWORDS) or label_norm.startswith("if yes"):
        is_req = False
        try:
            is_req = "*" in label or "required" in label.lower() or await elem.evaluate("""el => {
                return el.getAttribute('required') !== null || el.getAttribute('aria-required') === 'true';
            }""")
        except Exception:
            is_req = "*" in label
        
        if is_req:
            try:
                await set_field_value(elem, "N/A", frame=frame, label=label)
                log_field_decision(job_logger, label, classification, "hardcoded-conditional-if-yes (answered No -> N/A)", "N/A")
                return True
            except Exception as e:
                job_logger.error(f"Failed to fill required conditional field '{label}': {e}")
                return False
        else:
            job_logger.info(f"Conditional follow-up field '{label}' skipped because answer to parent question is No.")
            log_field_decision(job_logger, label, classification, "skipped-conditional-if-yes (answered No)", "")
            return True

    if matched_key in ("location", "city") or any(kw in label_norm for kw in ("location", "city", "where are you based", "currently based", "residence")) or ("where are you" in label_norm and "based" in label_norm):
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
        
        # Phone normalization: if inside an international tel widget with a country code prefix (e.g. iti),
        # strip the country code from the number to avoid duplicate prefix errors
        if value and (matched_key == "phone" or "phone" in label_norm or elem_type == "tel"):
            try:
                is_iti = await elem.evaluate("""el => {
                    return (el.className && el.className.includes('iti__tel-input')) || el.closest('.iti') !== null;
                }""")
            except Exception:
                is_iti = False
            
            if is_iti:
                # Strip leading +1 or other dial codes so only the national number is typed
                cleaned_phone = re.sub(r"^\+1\s*", "", str(value).strip())
                cleaned_phone = re.sub(r"^\+[\d]{1,3}\s*", "", cleaned_phone)
                value = cleaned_phone

        if not value:
            # Check if this field is required on the page
            is_req = False
            try:
                is_req = "*" in label or "required" in label.lower() or await elem.evaluate("""el => {
                    return el.getAttribute('required') !== null || el.getAttribute('aria-required') === 'true' || el.getAttribute('aria-invalid') === 'true';
                }""")
            except Exception:
                is_req = "*" in label or "required" in label.lower()

            if is_req:
                first_name = profile.get("first_name") or (profile.get("full_name") or "").split()[0] or "candidate"
                last_name = profile.get("last_name") or ((profile.get("full_name") or "").split()[-1] if len((profile.get("full_name") or "").split()) > 1 else "")
                name_slug = f"{first_name.lower()}-{last_name.lower()}".strip("-")
                
                if matched_key == "linkedin" or "linkedin" in label_norm:
                    value = f"https://www.linkedin.com/in/{name_slug}"
                elif matched_key == "github" or "github" in label_norm:
                    value = f"https://github.com/{first_name.lower()}{last_name.lower()}"
                elif matched_key in ("portfolio", "website") or any(w in label_norm for w in ("portfolio", "website")):
                    value = f"https://www.linkedin.com/in/{name_slug}"
                elif matched_key == "twitter" or "twitter" in label_norm:
                    value = "N/A"
                elif matched_key in ("facebook", "instagram", "youtube"):
                    value = "N/A"
                else:
                    value = await ask_ollama_open_ended(label, profile, job_logger, "OPEN_ENDED")

            if not value:
                # middle_name, secondary_last_name, social profiles, website/portfolio specifically: expected to be absent for many candidates.
                # Log at INFO, not WARNING, and leave the field blank — never Ollama.
                if matched_key in ("middle_name", "secondary_last_name", "facebook", "instagram", "youtube", "twitter", "github", "portfolio"):
                    job_logger.info(f"No {matched_key} in profile — leaving '{label}' blank.")
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
            await set_field_value(elem, value, frame=frame, label=label)
            log_field_decision(job_logger, label, classification, "profile.json", value)

            # If this is a confirmation email field, also ensure the main email field has the same value and triggers blur
            if "confirm" in label_norm and "email" in label_norm:
                try:
                    email_inputs = await frame.query_selector_all("input[type='email'], input#email-input, input[name*='email']:not([name*='confirm'])")
                    for em_el in email_inputs:
                        em_lbl = (await get_field_label(frame, em_el)).lower()
                        if "confirm" not in em_lbl and "email" in em_lbl:
                            curr_v = await read_element_value(em_el)
                            if not curr_v or curr_v.strip().lower() != str(value).strip().lower():
                                await set_field_value(em_el, value, frame=frame, label=em_lbl)
                except Exception:
                    pass

            return True
        except Exception as e:
            job_logger.error(f"Failed to fill text field '{label}': {e}")
            return False

    # --- Permanent hardcoded rules for text fields ---
    label_norm = normalize_text(label)
    if "pronoun" in label_norm:
        val = "He/Him"
        try:
            await set_field_value(elem, val, frame=frame, label=label)
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
            label_lower = label.lower()
            is_url_field = any(kw in label_lower for kw in ("url", "link", "http", "website", "portfolio", "github", "linkedin", "credential"))
            if is_url_field:
                job_logger.info(f"Ollama returned N/A for URL field '{label}'. Writing 'N/A' to satisfy required field.")
                try:
                    await set_field_value(elem, "N/A", frame=frame, label=label)
                    log_field_decision(job_logger, label, classification, "hardcoded-na (no url in profile)", "N/A")
                    return True
                except Exception as e:
                    job_logger.error(f"Failed to write N/A into URL field '{label}': {e}")
                    return False
            
            # Non-URL open-ended questions should not be left empty
            if any(term in label_lower for term in ["how did you hear", "hear about", "referral", "source"]):
                val = "LinkedIn"
            else:
                val = "I am enthusiastic about this role and look forward to contributing my technical skills."
            job_logger.info(f"Ollama returned N/A for non-URL field '{label}'. Using fallback answer: '{val}'")
    except Exception as e:
        job_logger.error(f"Ollama failed to answer open-ended question '{label}': {e}")
        return False

    if numeric_required:
        val = parse_numeric_value(val, label=label, elem_type=elem_type, input_mode=input_mode)

    try:
        await set_field_value(elem, val, frame=frame, label=label)
        log_field_decision(job_logger, label, classification, "ollama (numeric)" if numeric_required else "ollama", val)
        return True
    except Exception as e:
        job_logger.error(f"Failed to fill text field '{label}': {e}")
        return False



async def fill_select_field(elem: ElementHandle, label: str, profile: dict, job_logger, field_attempts: dict = None) -> bool:
    """Fills a native <select> dropdown by choosing an existing option."""
    if not label or not label.strip():
        return False

    existing_value = await read_element_value(elem)
    # Only treat as already-filled if the value is NOT a placeholder text
    if existing_value and normalize_text(existing_value) not in _SELECT_PLACEHOLDER_TEXTS:
        return True

    if field_attempts is not None and label:
        lbl_lower = label.lower()
        field_attempts[lbl_lower] = field_attempts.get(lbl_lower, 0) + 1
        if field_attempts[lbl_lower] > 3:
            job_logger.warning(f"Skipping select field '{label}' - exceeded max fill attempts (3).")
            return False

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
        elif any(kw in label_norm for kw in AGE_LABEL_KEYWORDS):
            for opt in options_texts:
                if AGE_ANSWER in normalize_text(opt):
                    selected_option_text = opt
                    source = f"hardcoded-rule (age: {AGE_ANSWER})"
                    break
        elif any(kw in label_norm for kw in RACE_LABEL_KEYWORDS):
            for ans_kw in RACE_ANSWER_KEYWORDS:
                for opt in options_texts:
                    if ans_kw in normalize_text(opt):
                        selected_option_text = opt
                        source = "hardcoded-rule (race: decline)"
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
            elif resolve_gender_identity_choice(label_norm, options_texts):
                selected_option_text = resolve_gender_identity_choice(label_norm, options_texts)
                source = "hardcoded-rule (gender identity)"
            elif resolve_hispanic_latino_choice(label_norm, options_texts):
                selected_option_text = resolve_hispanic_latino_choice(label_norm, options_texts)
                source = "hardcoded-rule (hispanic/latino: no)"
            elif resolve_eeo_race_choice(label_norm, options_texts):
                selected_option_text = resolve_eeo_race_choice(label_norm, options_texts)
                source = "hardcoded-rule (race: white/decline)"
            elif resolve_hear_about_us_choice(label_norm, options_texts):
                selected_option_text = resolve_hear_about_us_choice(label_norm, options_texts)
                source = "hardcoded-rule (hear about us: job board/linkedin)"
            elif resolve_skills_experience_choice(label_norm, options_texts, profile):
                selected_option_text = resolve_skills_experience_choice(label_norm, options_texts, profile)
                source = "profile.json (skills/resume match)"
            elif profile_value:
                best_text, score = best_matching_option(profile_value, options_texts)
                if best_text and score >= MATCH_THRESHOLD:
                    selected_option_text = best_text
                    source = "dropdown match"

        if not selected_option_text:
            # Strip garbled/control characters from label before sending to Ollama
            clean_label = re.sub(r'[\x00-\x1f\x7f-\x9f\ufffd\uFFFD]', '', label).strip()
            selected_option_text = await ask_ollama_choice(clean_label, options_texts, profile, job_logger, classification)
            source = "ollama"

        # If Ollama still returned a placeholder, pick the first real non-placeholder option
        if not selected_option_text or normalize_text(selected_option_text) in _SELECT_PLACEHOLDER_TEXTS:
            for opt_txt in options_texts:
                if normalize_text(opt_txt) not in _SELECT_PLACEHOLDER_TEXTS:
                    selected_option_text = opt_txt
                    source = "fallback-first-non-placeholder-option"
                    break

        if not selected_option_text:
            job_logger.warning(f"No valid option found for select '{label}' - all options are placeholders. Skipping.")
            return False

        matching_value = None
        final_text = selected_option_text
        for opt in options_data:
            if opt["text"].strip().lower() == selected_option_text.strip().lower() or selected_option_text.strip().lower() in opt["text"].strip().lower():
                matching_value = opt["value"]
                final_text = opt["text"]
                break

        if matching_value is None:
            # Never fall back to options_data[0] if it is a placeholder
            for opt in options_data:
                if normalize_text(opt["text"]) not in _SELECT_PLACEHOLDER_TEXTS:
                    matching_value = opt["value"]
                    final_text = opt["text"]
                    source = "fallback-first-non-placeholder-option"
                    break
            if matching_value is None:
                job_logger.warning(f"Could not find matching option value for '{label}'. Skipping.")
                return False

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

    if any(kw in label_norm for kw in VETERAN_LABEL_KEYWORDS) or any("protected veteran" in normalize_text(opt) for opt in options_texts):
        for ans_kw in VETERAN_ANSWER_KEYWORDS:
            for elem, opt_text in options:
                if ans_kw in normalize_text(opt_text):
                    await elem.scroll_into_view_if_needed()
                    await elem.click()
                    return True, "hardcoded-rule (veteran: decline)", opt_text

    if any(kw in label_norm for kw in DISABILITY_LABEL_KEYWORDS) or any("disability" in normalize_text(opt) for opt in options_texts):
        for ans_kw in DISABILITY_ANSWER_KEYWORDS:
            for elem, opt_text in options:
                if ans_kw in normalize_text(opt_text):
                    await elem.scroll_into_view_if_needed()
                    await elem.click()
                    return True, "hardcoded-rule (disability: decline)", opt_text

    if any(kw in label_norm for kw in AGE_LABEL_KEYWORDS) or any("30 39" in normalize_text(opt) for opt in options_texts):
        for elem, opt_text in options:
            if AGE_ANSWER in normalize_text(opt_text) or "30 39" in normalize_text(opt_text):
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, f"hardcoded-rule (age: {AGE_ANSWER})", opt_text

    if any(kw in label_norm for kw in RACE_LABEL_KEYWORDS) or any("hispanic" in normalize_text(opt) for opt in options_texts):
        for ans_kw in RACE_ANSWER_KEYWORDS:
            for elem, opt_text in options:
                if ans_kw in normalize_text(opt_text):
                    await elem.scroll_into_view_if_needed()
                    await elem.click()
                    return True, "hardcoded-rule (race: decline)", opt_text

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

    gender_id_choice = resolve_gender_identity_choice(label_norm, options_texts)
    if gender_id_choice:
        for elem, opt_text in options:
            if opt_text == gender_id_choice:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "hardcoded-rule (gender identity)", opt_text

    hispanic_choice = resolve_hispanic_latino_choice(label_norm, options_texts)
    if hispanic_choice:
        for elem, opt_text in options:
            if opt_text == hispanic_choice:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "hardcoded-rule (hispanic/latino: no)", opt_text

    eeo_race_choice = resolve_eeo_race_choice(label_norm, options_texts)
    if eeo_race_choice:
        for elem, opt_text in options:
            if opt_text == eeo_race_choice:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "hardcoded-rule (race: white/decline)", opt_text

    source_choice = resolve_hear_about_us_choice(label_norm, options_texts)
    if source_choice:
        for elem, opt_text in options:
            if opt_text == source_choice:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "hardcoded-rule (hear about us: job board/linkedin)", opt_text

    skill_choice = resolve_skills_experience_choice(label_norm, options_texts, profile)
    if skill_choice:
        for elem, opt_text in options:
            if opt_text == skill_choice:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "profile.json (skills/resume match)", opt_text

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
            try:
                await elem.click(force=True)
            except Exception:
                await elem.click()
            return True, source, opt_text

    try:
        await options[0][0].click(force=True)
    except Exception:
        await options[0][0].click()
    return True, "fallback-first-option", options[0][1]


async def fill_radio_group(frame, name_attr: str, label: str, profile: dict, job_logger, field_attempts: dict = None) -> bool:
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
                aria_labelledby = await r.get_attribute("aria-labelledby")
                if aria_labelledby:
                    ids = aria_labelledby.split()
                    texts = []
                    for lid in ids:
                        try:
                            lbl = await frame.query_selector(f"[id='{lid}']")
                            if lbl:
                                t = (await lbl.inner_text()).strip()
                                if t: texts.append(t)
                        except Exception:
                            pass
                    r_label = " ".join(texts)
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

        if field_attempts is not None and label:
            lbl_lower = label.lower()
            field_attempts[lbl_lower] = field_attempts.get(lbl_lower, 0) + 1
            if field_attempts[lbl_lower] > 3:
                job_logger.warning(f"Skipping radio group '{label}' - exceeded max fill attempts (3).")
                return False

        classification, _ = classify_field(label, "radio", profile)
        success, source, final_text = await _resolve_choice_and_click(radio_options, label, profile, job_logger, classification)
        if success:
            log_field_decision(job_logger, label, classification, source, final_text)
        return success
    except Exception as e:
        job_logger.error(f"Failed to fill radio group '{label}': {e}")
        return False


async def fill_aria_radio_group(frame, group_elem: ElementHandle, label: str, profile: dict, job_logger, field_attempts: dict = None) -> bool:
    """Fills an ARIA [role=radiogroup] or custom radiogroup component (e.g. spl-radio-group) made of [role=radio] or <spl-radio> children."""
    try:
        # Query both standard [role='radio'] and custom elements like spl-radio
        radios = await group_elem.query_selector_all("[role='radio'], spl-radio")
        if not radios:
            # Try piercing shadow root if children are slotted or inside shadow DOM
            try:
                shadow_radios = await group_elem.evaluate_handle("""el => {
                    const root = el.shadowRoot || el;
                    return Array.from(root.querySelectorAll('[role="radio"], spl-radio, input[type="radio"]'));
                }""")
                radios = await shadow_radios.as_element().query_selector_all("*") if shadow_radios else []
            except Exception:
                pass

        radio_options = []
        for r in radios:
            r_label = (await r.get_attribute("label")) or (await r.get_attribute("aria-label")) or ""
            if not r_label.strip():
                aria_labelledby = await r.get_attribute("aria-labelledby")
                if aria_labelledby:
                    ids = aria_labelledby.split()
                    texts = []
                    for lid in ids:
                        try:
                            lbl = await frame.query_selector(f"[id='{lid}']")
                            if lbl:
                                t = (await lbl.inner_text()).strip()
                                if t: texts.append(t)
                        except Exception:
                            pass
                    r_label = " ".join(texts)
            if not r_label.strip():
                r_label = (await r.inner_text()).strip()
            if not r_label.strip():
                # Try getting text content or value attribute
                r_val = (await r.get_attribute("value")) or ""
                if r_val == "1":
                    r_label = "Yes"
                elif r_val == "0":
                    r_label = "No"
            radio_options.append((r, r_label.strip()))

        # Overrides a page's pre-checked default (e.g. "I require assistance
        # immediately.") before the generic "already answered, skip" check
        # below mistakes it for an answer the bot already gave.
        options_texts = [text for _, text in radio_options if text]
        forced_choice = resolve_visa_sponsorship_choice(normalize_text(label), options_texts, profile)
        if forced_choice:
            for r, opt_text in radio_options:
                if opt_text == forced_choice:
                    is_checked = (await r.get_attribute("aria-checked")) == "true" or (await r.get_attribute("checked")) is not None
                    if is_checked:
                        return True
                    await r.scroll_into_view_if_needed()
                    await r.click()
                    log_field_decision(job_logger, label, "CHOICE_FIELD",
                        "profile.json (visa sponsorship not needed -> legally authorized statement, overriding page default)", forced_choice)
                    return True

        for r, _ in radio_options:
            is_checked = (await r.get_attribute("aria-checked")) == "true" or (await r.get_attribute("checked")) is not None
            if is_checked:
                return True

        if field_attempts is not None and label:
            lbl_lower = label.lower()
            field_attempts[lbl_lower] = field_attempts.get(lbl_lower, 0) + 1
            if field_attempts[lbl_lower] > 3:
                job_logger.warning(f"Skipping ARIA radio group '{label}' - exceeded max fill attempts (3).")
                return False

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

    # "Prefer not to answer" / "Decline" standalone checkboxes
    if any(kw in label_norm for kw in ("prefer not to answer", "prefer not to say", "decline to answer", "decline to self identify", "do not wish to")):
        return True, "hardcoded-rule (prefer not to answer)"

    # Mandatory privacy / policy / declaration / terms checkboxes
    if any(kw in label_norm for kw in ("privacy notice", "terms and conditions", "terms & conditions", "i agree", "i declare", "acknowledge", "declaration", "consent to", "privacy policy")):
        return True, "hardcoded-rule (privacy/consent/terms: agree)"

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
                       "ze hir", "ey em", "hir hir", "fae faer", "hu hu", 
                       "use name only", "custom", "prefer not to say", 
                       "decline to answer", "decline to self identify"]
    if label_norm in pronoun_options:
        should_check = label_norm in [normalize_text(p) for p in PRONOUNS_PREFERRED]
        return should_check, "hardcoded-rule (pronouns: He/Him only)"

    # Race/Ethnicity standalone checkboxes (uncheck specific races since we always decline)
    race_options_keywords = [
        "white caucasian", "hispanic latino", "spanish origin", "black or african",
        "native hawaiian", "pacific islander", "indigenous people", "alaska native",
        "middle eastern", "north african", "some other race", "two or more races",
        "american indian"
    ]
    if label_norm == "asian" or any(kw in label_norm for kw in race_options_keywords):
        return False, "hardcoded-rule (race: decline, returning false for specific race)"

    # Age standalone checkboxes (uncheck non-30-39 ages)
    age_options_keywords = [
        "17 or younger", "18 20", "21 29", "40 49", "50 59", "60 or older"
    ]
    if any(kw in label_norm for kw in age_options_keywords):
        return False, "hardcoded-rule (age: decline, returning false for non-30-39 age)"

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


async def fill_checkbox(elem: ElementHandle, label: str, profile: dict, job_logger, field_attempts: dict = None) -> bool:
    """Handles a native <input type=checkbox> (e.g. Terms, equal opportunity, relocation, etc.)."""
    try:
        classification, _ = classify_field(label, "checkbox", profile)
        should_check, source = await _resolve_checkbox_state(label, profile, job_logger)

        is_currently_checked = await elem.is_checked()
        if should_check == is_currently_checked:
            return True

        if field_attempts is not None and label:
            lbl_lower = label.lower()
            field_attempts[lbl_lower] = field_attempts.get(lbl_lower, 0) + 1
            if field_attempts[lbl_lower] > 3:
                job_logger.warning(f"Skipping checkbox '{label}' - exceeded max fill attempts (3).")
                return False

        # Pronouns: the rule returns False for all non-He/Him options, so
        # explicitly uncheck them if they were pre-selected.
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


async def fill_aria_checkbox(elem: ElementHandle, label: str, profile: dict, job_logger, field_attempts: dict = None) -> bool:
    """Handles a custom-widget [role=checkbox] element (toggled via click + aria-checked, not .check())."""
    try:
        classification, _ = classify_field(label, "checkbox", profile)
        should_check, source = await _resolve_checkbox_state(label, profile, job_logger)

        # Check if the element itself or its shadowRoot / internal input is already checked
        current_state = await elem.evaluate("""el => {
            if (el.getAttribute('aria-checked') === 'true' || el.checked === true) return true;
            if (el.shadowRoot) {
                const inner = el.shadowRoot.querySelector('input[type="checkbox"], [role="checkbox"]');
                if (inner && (inner.getAttribute('aria-checked') === 'true' || inner.checked === true)) return true;
            }
            const child = el.querySelector('input[type="checkbox"], [role="checkbox"]');
            if (child && (child.getAttribute('aria-checked') === 'true' || child.checked === true)) return true;
            return false;
        }""")
        
        if should_check == current_state:
            return True

        if field_attempts is not None and label:
            lbl_lower = label.lower()
            field_attempts[lbl_lower] = field_attempts.get(lbl_lower, 0) + 1
            if field_attempts[lbl_lower] > 3:
                job_logger.warning(f"Skipping ARIA checkbox '{label}' - exceeded max fill attempts (3).")
                return False

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
        has_pressed = await container.evaluate("el => Array.from(el.querySelectorAll('button')).some(b => b.getAttribute('aria-pressed') === 'true')")
        if has_pressed:
            return True
        classes = await container.evaluate("el => Array.from(el.querySelectorAll('button')).map(b => b.className)")
        return len(set(classes)) > 1
    except Exception:
        return False


async def fill_yesno_buttons(frame, container: ElementHandle, label: str, profile: dict, job_logger, max_attempts: int = 3, field_attempts: dict = None) -> bool:
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

    if field_attempts is not None and label:
        lbl_lower = label.lower()
        field_attempts[lbl_lower] = field_attempts.get(lbl_lower, 0) + 1
        if field_attempts[lbl_lower] > 3:
            job_logger.warning(f"Skipping Yes/No buttons '{label}' - exceeded max fill attempts (3).")
            return False

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


async def handle_file_upload(elem: ElementHandle, label: str, profile: dict, job_logger, resume_already_uploaded: bool = False) -> bool:
    """
    Uploads the resume or cover letter file. Deliberately does not
    require the input to be visible - drag-and-drop uploaders commonly hide the
    real <input type=file> behind a styled dropzone, and set_input_files works
    on hidden inputs regardless.
    """
    try:
        # If this input already has a file attached, do not re-upload
        try:
            has_files = await elem.evaluate("el => el.files && el.files.length > 0")
            if has_files:
                return True
        except Exception:
            pass

        label_lower = (label or "").lower()
        
        # Fallback: inspect DOM attributes (e.g. name, id, data-testid, aria-label, parent classes)
        dom_hints = await elem.evaluate("""el => {
            let lbl = el.closest('label');
            let parent = el.closest('div, section, form, fieldset');
            return [
                el.id || '', el.name || '', el.getAttribute('data-testid') || '', el.getAttribute('aria-label') || '',
                el.getAttribute('accept') || '', el.className || '',
                lbl ? (lbl.getAttribute('data-testid') || '') + ' ' + (lbl.getAttribute('aria-label') || '') + ' ' + (lbl.innerText || '') : '',
                parent ? (parent.className || '') + ' ' + (parent.getAttribute('data-testid') || '') + ' ' + (parent.innerText || '') : ''
            ].join(' ').toLowerCase();
        }""")
        combined_text = (label_lower + " " + dom_hints).strip()

        # If this is strictly an 'Autofill from resume' parser dropzone (like on Ashby),
        # skip it so we don't trigger form re-rendering/wipes or misidentify it as the actual application attachment.
        if any(kw in combined_text for kw in ["autofill", "auto-fill", "autofill from resume", "autofill key application fields", "to autofill key"]):
            job_logger.info(f"Skipping autofill parser dropzone (label='{label}') to preserve form state")
            return False

        if "cover letter" in combined_text or "cover_letter" in combined_text:
            try:
                cover_letter_path = get_or_create_cover_letter(profile, job_logger)
            except Exception as e:
                job_logger.error(f"Failed to generate cover letter for field '{label}': {e}")
                return False
            absolute_path = os.path.abspath(cover_letter_path)
            await elem.set_input_files(absolute_path)
            job_logger.info(f"Uploaded generated cover letter '{absolute_path}' to field '{label}'")
            return True
        else:
            is_explicit_resume = any(kw in combined_text for kw in ["resume", "cv", "curriculum vitae", "_systemfield_resume"])
            is_required_file = False
            try:
                is_required_file = await elem.evaluate("el => el.required || el.getAttribute('aria-required') === 'true' || (el.closest('div, section') && (el.closest('div, section').className||'').toLowerCase().includes('required'))")
            except Exception:
                pass

            if is_explicit_resume:
                resume_path = profile.get("resume_file_path", "")
                if resume_path and os.path.exists(resume_path):
                    absolute_path = os.path.abspath(resume_path)
                    await elem.set_input_files(absolute_path)
                    job_logger.info(f"Uploaded resume file '{absolute_path}' to file upload field (label='{label}')")
                    return True
                else:
                    job_logger.error(f"Resume file path '{resume_path}' is invalid or file does not exist.")
            elif is_required_file:
                if resume_already_uploaded:
                    job_logger.info(f"Resume already uploaded earlier; skipping additional required file upload field (label='{label}')")
                    return True
                resume_path = profile.get("resume_file_path", "")
                if resume_path and os.path.exists(resume_path):
                    absolute_path = os.path.abspath(resume_path)
                    await elem.set_input_files(absolute_path)
                    job_logger.info(f"Uploaded resume file '{absolute_path}' to required file upload field (label='{label}')")
                    return True
            else:
                job_logger.info(f"Skipping unclassified optional file upload field (label='{label}')")
                return False
    except Exception as e:
        job_logger.error(f"Failed to upload file to field '{label}': {e}")
    return False


async def find_and_click_next_button(frame, fields_found: int = 1) -> bool:
    """
    Searches for multi-step buttons like 'Next', 'Continue', 'Proceed', 'Next Step'
    and clicks them if found. Returns True if button was clicked.
    Deliberately excludes 'Apply' / 'Apply Now' buttons so single-page forms don't
    re-click the top apply button and loop indefinitely.
    """
    next_selectors = [
        "spl-button:has-text('Next')", "spl-button:has-text('Continue')", "spl-button:has-text('Proceed')",
        "button:has-text('Next Step')", "button:has-text('Next step')",
        "button:has-text('Next')", "button:has-text('Continue')",
        "button:has-text('Proceed')", "input[type='button'][value='Next']",
        "input[type='button'][value='Continue']", "a:has-text('Next Step')",
        "a:has-text('Next')", "button[id*='next']:not([id*='prev'])",
        "button[class*='next']:not([class*='prev'])",
        "button[data-automation-id*='next']", "button[data-automation-id*='bottom-navigation-next-button']",
        "button[data-qa*='next']", "button[data-testid*='next']",
        "button:has-text('Save & Continue')", "button:has-text('Save and Continue')",
        "button:has-text('Save and continue')", "a:has-text('Save & Continue')",
        "button:has-text('Review Application')", "button:has-text('Next section')",
        "button:has-text('I Confirm')", "a:has-text('I Confirm')", "input[value='I Confirm']",
        "button:has-text('I Accept')", "a:has-text('I Accept')", "input[value='I Accept']",
        "button:has-text('Agree')", "a:has-text('Agree')", "input[value='Agree']",
        "[role='button']:has-text('Next')", "[role='button']:has-text('Continue')"
    ]
        
    for selector in next_selectors:
        try:
            btn = frame.locator(selector).first
            if await btn.count() and await btn.is_visible() and await btn.is_enabled():
                btn_type = (await btn.get_attribute("type") or "").lower()
                btn_text = (await btn.inner_text() or "").strip().lower()
                # Make sure it's not a submit button
                if btn_type == "submit" or any(term in btn_text for term in ["submit", "finish"]):
                    continue

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
        # Generic error banner detection
        banner_texts = await frame.evaluate("""() => {
            const texts = [];
            const elems = document.querySelectorAll('.error-message, .alert-danger, [role="alert"], .application-error, .form-error, .error');
            for (const el of elems) {
                if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                    texts.push(el.innerText);
                }
            }
            return texts;
        }""")
        for text in banner_texts:
            if text and len(text.strip()) > 0 and ("error" in text.lower() or "correction" in text.lower() or "missing" in text.lower() or "required" in text.lower()):
                reasons.append(f"Form error banner: {text.strip()}")
    except Exception:
        pass

    try:
        invalid_elems = await frame.query_selector_all("[aria-invalid='true']")
        for elem in invalid_elems:
            if await elem.is_visible():
                label = await get_field_label(frame, elem)
                reasons.append(f"Field '{label or 'unknown'}' marked aria-invalid")
    except Exception:
        pass

    try:
        required_elems = await frame.query_selector_all(
            "input[required]:not([type='hidden']), select[required], textarea[required], "
            "input[aria-required='true']:not([type='hidden']), select[aria-required='true'], textarea[aria-required='true']"
        )
        for elem in required_elems:
            if await elem.is_visible():
                # Skip checkboxes — their "value" is irrelevant; .checked state matters
                elem_type = await elem.evaluate("el => (el.type || '').toLowerCase()")
                if elem_type == "checkbox":
                    continue

                is_file = await elem.evaluate("el => el.type === 'file'")
                if is_file:
                    has_files = await elem.evaluate("el => el.files && el.files.length > 0")
                    if not has_files:
                        label = await get_field_label(frame, elem)
                        reasons.append(f"Required file upload '{label or 'Resume'}' is empty")
                    continue

                if elem_type == "radio":
                    name = await elem.get_attribute("name")
                    if name:
                        is_group_checked = await frame.evaluate(f"() => !!document.querySelector('input[type=\"radio\"][name=\"{name}\"]:checked')")
                        if not is_group_checked:
                            label = await get_field_label(frame, elem)
                            if label and label.strip():
                                reasons.append(f"Required radio group '{label}' has no selection")
                    continue

                # Call read_element_value directly instead of duplicating JS logic
                value = await read_element_value(elem)
                if not value or not str(value).strip():
                    label = await get_field_label(frame, elem)
                    # Only flag if we have a real label — unlabeled hidden inputs (e.g. inside custom
                    # checkbox components) are not real empty fields, they are framework internals.
                    if label and label.strip():
                        reasons.append(f"Required field '{label}' is empty")
    except Exception:
        pass

    # Specifically check if the application's Resume file input is empty
    try:
        file_inputs = await frame.query_selector_all("input[type='file']")
        for fi in file_inputs:
            fi_info = await fi.evaluate("""el => {
                let req = el.required || el.getAttribute('aria-required') === 'true';
                let parent = el.closest('div, section, form, fieldset');
                let parentText = (parent ? parent.innerText : '').toLowerCase();
                let isResume = (el.id && el.id.includes('resume')) || parentText.includes('resume') || parentText.includes('cv');
                let isAutofill = parentText.includes('autofill') || parentText.includes('auto-fill');
                let hasFiles = el.files && el.files.length > 0;
                let isReq = req || parentText.includes('*') || (parent && (parent.className || '').toLowerCase().includes('required'));
                return { isResume, isAutofill, isReq, hasFiles };
            }""")
            if fi_info["isResume"] and not fi_info["isAutofill"] and (fi_info["isReq"] or True):
                if not fi_info["hasFiles"]:
                    label = await get_field_label(frame, fi)
                    reasons.append(f"Required file upload '{label or 'Resume'}' is empty")
    except Exception:
        pass

    # Check for uncompleted required Yes/No button pairs
    try:
        yesno_groups = await find_yesno_button_groups(frame)
        for yg in yesno_groups:
            answered = await is_yesno_already_answered(yg)
            if not answered:
                is_req = await yg.evaluate("""el => {
                    let parent = el.closest('div, section, form, fieldset');
                    if (parent) {
                        let pCls = (parent.className || '').toLowerCase();
                        if (pCls.includes('required')) return true;
                        let lbl = parent.querySelector('label, legend, h3, h4, h5, p');
                        if (lbl && (((lbl.className || '').toLowerCase().includes('required')) || (lbl.innerText || '').includes('*'))) return true;
                    }
                    return false;
                }""")
                if is_req:
                    label = await get_field_label(frame, yg, is_group=True)
                    reasons.append(f"Required Yes/No question '{label or 'Unknown'}' is not answered")
    except Exception:
        pass

    try:
        error_elems = await frame.query_selector_all("[class*='error'], [class*='invalid']")
        for elem in error_elems:
            if await elem.is_visible():
                is_form_control_or_host = await elem.evaluate("""el => {
                    if (el.shadowRoot || el.tagName.includes('-')) return true;
                    if (['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(el.tagName)) return true;
                    return el.querySelector('input, select, textarea, button, a, li, label, [role]') !== null;
                }""")
                if not is_form_control_or_host:
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





class FormBlockedException(Exception):
    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(f"{status}: {reason}")



async def fill_aria_invalid_fields(frame, profile: dict, job_logger, field_attempts: dict = None) -> int:
    """
    Targeted recovery pass: finds all fields marked aria-invalid (reported by
    the server after a failed submit), scrolls each one into view so off-screen
    or below-the-fold fields become reachable, then fills them.
    This is intentionally narrow — it only touches broken fields, not the whole form.
    """
    filled = 0
    try:
        invalid_elems = await frame.query_selector_all("[aria-invalid='true']")
        job_logger.info(f"Targeted recovery: found {len(invalid_elems)} aria-invalid field(s).")
        for elem in invalid_elems:
            try:
                # Scroll the element into view first — this is critical for off-screen fields
                try:
                    await elem.scroll_into_view_if_needed(timeout=3000)
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

                tag = (await elem.evaluate("el => el.tagName.toLowerCase()")).lower()
                type_attr = (await elem.get_attribute("type") or "").lower()
                outer_html = await elem.evaluate("el => el.outerHTML")
                job_logger.info(f"Targeted recovery DEBUG: found aria-invalid element: {outer_html[:200]}...")
                label = await get_field_label(frame, elem)

                if not label:
                    job_logger.warning(f"Targeted recovery DEBUG: could not extract label for aria-invalid element.")
                    continue

                job_logger.info(f"Targeted recovery: filling aria-invalid field '{label}' (tag={tag}, type={type_attr})")

                if tag == "select" or "select" in tag or "combobox" in tag or "autocomplete" in tag:
                    await fill_select_field(elem, label, profile, job_logger, field_attempts)
                    filled += 1
                elif tag == "input" and type_attr == "radio":
                    name_attr = await elem.get_attribute("name") or ""
                    if name_attr:
                        await fill_radio_group(frame, name_attr, label, profile, job_logger, field_attempts)
                        filled += 1
                elif tag in ("textarea", "spl-input", "spl-textarea") or (tag == "input" and type_attr not in ("radio", "checkbox", "file", "hidden", "submit", "button", "password")):
                    # For text inputs, determine the right value from profile
                    classification, matched_key = classify_field(label, "text", profile)
                    value_to_type = get_profile_value(profile, matched_key) if matched_key else None

                    # If no profile key matched but the field looks like a location field, use location
                    label_lower = label.lower()
                    if not value_to_type and (
                        "based" in label_lower or "location" in label_lower or "city" in label_lower
                        or "where are you" in label_lower or "residence" in label_lower
                    ):
                        value_to_type = profile.get("location") or profile.get("city") or ""

                    if value_to_type:
                        # Direct force-type: React/Greenhouse controlled inputs require keyboard events.
                        # Using .type() (character by character) registers through React's synthetic event
                        # system, unlike .fill() which sets the DOM value directly and bypasses React state.
                        try:
                            await elem.click(timeout=2000)
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
                        try:
                            await elem.type(str(value_to_type), delay=30, timeout=5000)
                        except Exception:
                            try:
                                await elem.fill(str(value_to_type), timeout=2000)
                            except Exception:
                                pass
                        # Fire React-compatible events
                        try:
                            await elem.evaluate("""el => {
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('blur', { bubbles: true }));
                            }""")
                        except Exception:
                            pass
                        try:
                            await elem.press("Tab", timeout=1000)
                        except Exception:
                            pass
                        await asyncio.sleep(0.3)
                        committed = await elem.evaluate("el => el.value || ''")
                        log_field_decision(job_logger, label, classification or "PROFILE_FIELD", "targeted-recovery-type", committed or value_to_type)
                        filled += 1
                    else:
                        # Fall back to standard fill_text_field for non-location fields
                        await fill_text_field(frame, elem, label, profile, job_logger, field_attempts)
                        filled += 1
            except Exception as e:
                job_logger.warning(f"Targeted recovery: error filling aria-invalid field: {e}")
    except Exception as e:
        job_logger.warning(f"Targeted recovery scan failed: {e}")
    return filled


async def process_form_fields(frame, profile: dict, job_logger, resume_already_uploaded: bool = False, field_attempts: dict = None) -> int:
    """
    Enumerates every field type in the current step and fills them in a single
    pass ordered by on-page vertical position - top to bottom. Using a unified
    selector guarantees that if bounding box detection fails, elements fall back
    to their strict DOM order (which matches visual top-to-bottom layout), rather
    than grouping by field type.
    """
    tasks = []  # (discovery_order, kind, elem, extra, initial_id, initial_name)
    order_counter = 0
    resume_uploaded = resume_already_uploaded

    async def _add(elem, kind, extra=None):
        nonlocal order_counter
        order_counter += 1
        elem_id = ""
        elem_name = ""
        try:
            elem_id = (await elem.get_attribute("id") or "").strip()
            elem_name = (await elem.get_attribute("name") or "").strip()
        except Exception:
            pass
        tasks.append((order_counter, kind, elem, extra, elem_id, elem_name))

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
            is_hidden_helper = await elem.evaluate("""el => {
                if (el.tagName === 'INPUT' && el.type !== 'file' && el.type !== 'radio' && el.type !== 'checkbox') {
                    if (el.getAttribute('tabindex') === '-1' || el.getAttribute('aria-hidden') === 'true') return true;
                    if (el.className && el.className.includes('requiredInput')) return true;
                }
                const name = (el.name || '').toLowerCase();
                const tid = (el.getAttribute('data-testid') || '').toLowerCase();
                const ph = (el.placeholder || '').toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                const id = (el.id || '').toLowerCase();
                const catalogKeywords = [
                    'search-jobs', 'search jobs', 'all departments', 'all job types',
                    'all locations', 'search by title', 'search by keyword', 'filter by',
                    'select-search-input', 'search locations', 'jobs-filter', 'job-search'
                ];
                if (catalogKeywords.some(kw => name.includes(kw) || tid.includes(kw) || ph.includes(kw) || aria.includes(kw) || id.includes(kw))) {
                    return true;
                }
                return false;
            }""")
            if is_hidden_helper:
                continue
            tag = await elem.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            continue
            
        role = (await elem.get_attribute("role") or "").lower()
        type_attr = (await elem.get_attribute("type") or "").lower()
        contenteditable = (await elem.get_attribute("contenteditable") or "").lower()

        # File inputs (Ashby, Lever, Greenhouse, etc.) are frequently hidden behind custom dropzones.
        # Process them without visibility restrictions.
        if tag == "input" and type_attr == "file":
            await _add(elem, "file")
        elif tag == "input" and type_attr == "radio":
            if await elem.is_visible():
                name_attr = await elem.get_attribute("name")
                if name_attr and name_attr not in processed_radio_names:
                    processed_radio_names.add(name_attr)
                    await _add(elem, "radio", name_attr)
        elif tag == "input" and type_attr == "checkbox":
            if await elem.is_visible():
                await _add(elem, "checkbox")
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
        elif tag == "textarea" or contenteditable == "true" or (tag == "input" and type_attr not in ("radio", "checkbox", "file", "hidden", "submit", "button", "password")):
            if await elem.is_visible():
                await _add(elem, "text")

    # Extra guarantee: query all input[type='file'] on the frame so no hidden uploaders are missed,
    # ensuring no duplicates are added if already discovered by giant_selector.
    try:
        all_file_inputs = await frame.query_selector_all("input[type='file']")
        for fi in all_file_inputs:
            already_added = False
            for t in tasks:
                if t[1] == "file":
                    try:
                        same = await frame.evaluate("(a, b) => a === b", [t[2], fi])
                        if same:
                            already_added = True
                            break
                    except Exception:
                        pass
            if not already_added:
                await _add(fi, "file")
    except Exception:
        pass

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

    # SmartRecruiters custom web components discovery:
    # spl-input and spl-autocomplete are used for dropdowns/comboboxes (EEO, preliminary questions).
    # spl-radio-group is used for radio groups (work authorization, visa sponsorship, disability).
    # These do not expose their internal roles on the outer host element, so giant_selector misses them.
    try:
        existing_ids = set()
        for t in tasks:
            try:
                eid = await t[2].get_attribute("id") or ""
                if eid:
                    existing_ids.add(eid)
            except Exception:
                pass

        # 1. spl-input & spl-autocomplete as comboboxes
        spl_combos = await frame.query_selector_all("spl-input, spl-autocomplete")
        for spl_elem in spl_combos:
            try:
                if not await spl_elem.is_visible():
                    continue
                if await is_chat_or_support_element(spl_elem):
                    continue
                spl_id = (await spl_elem.get_attribute("id") or "").strip()
                if spl_id and spl_id in existing_ids:
                    continue  # already discovered
                await _add(spl_elem, "combobox")
                if spl_id:
                    existing_ids.add(spl_id)
                job_logger.debug(f"Discovered SmartRecruiters combobox element: id='{spl_id}'")
            except Exception:
                continue

        # 2. spl-radio-group as radiogroup
        spl_radios = await frame.query_selector_all("spl-radio-group")
        for spl_rg in spl_radios:
            try:
                if not await spl_rg.is_visible():
                    continue
                if await is_chat_or_support_element(spl_rg):
                    continue
                rg_id = (await spl_rg.get_attribute("id") or "").strip()
                if rg_id and rg_id in existing_ids:
                    continue
                await _add(spl_rg, "radiogroup")
                if rg_id:
                    existing_ids.add(rg_id)
                job_logger.debug(f"Discovered SmartRecruiters spl-radio-group: id='{rg_id}'")
            except Exception:
                continue
    except Exception:
        pass

    # Single top-to-bottom pass, ordered strictly by DOM discovery order.
    tasks.sort(key=lambda t: t[0])

    # Check all discovered fields before starting execution for OTP / security code
    for _, _, elem, _, _, _ in tasks:
        try:
            lbl = await get_field_label(frame, elem)
            lbl_lower = lbl.lower()
            if any(kw in lbl_lower for kw in ["security code", "verification code", "one-time", "confirm you're a human", "confirm you are a human"]):
                job_logger.warning(f"OTP / Security code field detected in form fields: '{lbl}'")
                raise FormBlockedException("OTP Required", f"Security code field detected on page: '{lbl}'")
        except FormBlockedException:
            raise
        except Exception:
            pass

    for _, kind, elem, extra, initial_id, initial_name in tasks:
        try:
            # Check if elem is still connected, re-acquire if detached by an earlier file upload/render
            try:
                is_connected = await elem.evaluate("el => el.isConnected === true")
            except Exception:
                is_connected = False

            if not is_connected:
                fresh = None
                if initial_id:
                    fresh = await frame.query_selector(f"[id='{initial_id}']")
                if not fresh and initial_name:
                    fresh = await frame.query_selector(f"[name='{initial_name}']")
                if fresh:
                    elem = fresh

            label = await get_field_label(frame, elem, is_group=(kind in ("radio", "radiogroup", "yesno")))
            if not is_connected and not label and initial_id:
                fresh = await frame.query_selector(f"[id='{initial_id}']")
                if fresh:
                    elem = fresh
                    label = await get_field_label(frame, elem, is_group=(kind in ("radio", "radiogroup", "yesno")))

            label_lower = label.lower()
            if any(kw in label_lower for kw in ["security code", "verification code", "one-time", "confirm you're a human", "confirm you are a human"]):
                job_logger.warning(f"OTP / Security code field detected: '{label}'")
                raise FormBlockedException("OTP Required", f"Security code field detected on page: '{label}'")

            if kind == "text":
                await fill_text_field(frame, elem, label, profile, job_logger, field_attempts)
                # If we just filled the email field, check if a dynamic OTP / security code section appeared on the page
                _, matched_key = classify_field(label, "text", profile)
                if matched_key == "email" or "email" in label_lower:
                    await asyncio.sleep(1.0)
                    pg = frame.page if hasattr(frame, 'page') else frame
                    block_status, block_reason = await detect_captcha_or_login_wall(pg)
                    if block_status:
                        job_logger.warning(f"Challenge emerged after entering email: {block_status} ({block_reason})")
                        raise FormBlockedException(block_status, block_reason)
            elif kind == "select":
                await fill_select_field(elem, label, profile, job_logger, field_attempts)
            elif kind == "combobox":
                _, matched_key = classify_field(label, "combobox", profile)
                await handle_combobox_field(frame, elem, label, profile, job_logger, matched_key, field_attempts)
            elif kind == "file":
                uploaded = await handle_file_upload(elem, label, profile, job_logger, resume_already_uploaded=resume_uploaded)
                if uploaded and not ("cover letter" in label_lower or "cover_letter" in label_lower):
                    resume_uploaded = True
                await wait_for_fields_to_settle(frame, timeout_ms=3000)
            elif kind == "radio":
                await fill_radio_group(frame, extra, label, profile, job_logger, field_attempts)
            elif kind == "radiogroup":
                group_label = (await elem.get_attribute("aria-label")) or label
                await fill_aria_radio_group(frame, elem, group_label, profile, job_logger, field_attempts)
            elif kind == "checkbox":
                await fill_checkbox(elem, label, profile, job_logger, field_attempts)
            elif kind == "aria_checkbox":
                await fill_aria_checkbox(elem, label, profile, job_logger, field_attempts)
            elif kind == "yesno":
                await fill_yesno_buttons(frame, elem, label, profile, job_logger, max_attempts=3, field_attempts=field_attempts)
        except FormBlockedException:
            raise
        except Exception as e:
            job_logger.warning(f"Skipping a '{kind}' field mid-pass due to an error (likely a stale element from a re-render earlier in this pass): {e}")

    # -----------------------------------------------------------------------
    # Final re-fill: "Confirm email" fields MUST be filled last.
    # SmartRecruiters clears the confirm-email field whenever any other field
    # on the same step triggers a React state update. Re-filling it at the
    # very end of every pass ensures it's populated when validation runs.
    # -----------------------------------------------------------------------
    email_val = str(profile.get("email", "")).strip()
    if email_val:
        for _, kind, elem, extra, initial_id, initial_name in tasks:
            try:
                if kind != "text":
                    continue
                lbl = await get_field_label(frame, elem, is_group=False)
                lbl_lower = lbl.lower()
                if "confirm" not in lbl_lower or "email" not in lbl_lower:
                    continue
                # Re-acquire element if detached
                try:
                    is_connected = await elem.evaluate("el => el.isConnected === true")
                except Exception:
                    is_connected = False
                if not is_connected:
                    fresh = None
                    if initial_id:
                        fresh = await frame.query_selector(f"[id='{initial_id}']")
                    if not fresh and initial_name:
                        fresh = await frame.query_selector(f"[name='{initial_name}']")
                    if fresh:
                        elem = fresh
                    else:
                        continue
                # Check current value — re-fill only if empty or mismatched
                curr_val = await read_element_value(elem)
                if curr_val and curr_val.strip().lower() == email_val.lower():
                    continue  # already correct, skip
                job_logger.info(f"Re-filling '{lbl}' at end of pass (cleared by site): '{email_val}'")
                # Pierce shadow DOM to reach the actual <input>
                try:
                    inner_handle = await elem.evaluate_handle("""el => {
                        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el;
                        function deepFind(root) {
                            if (!root) return null;
                            const inp = root.querySelector('input, textarea');
                            if (inp) return inp;
                            const all = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                            for (const ch of all) {
                                if (ch.shadowRoot) { const r = deepFind(ch.shadowRoot); if (r) return r; }
                            }
                            return null;
                        }
                        return deepFind(el.shadowRoot || el) || el;
                    }""")
                    target = inner_handle.as_element() if inner_handle else elem
                    await target.fill(email_val, force=True)
                    check = await target.evaluate("el => el.value")
                    if not check:
                        await target.focus()
                        await target.type(email_val, delay=40)
                except Exception:
                    try:
                        await elem.focus()
                        await elem.type(email_val, delay=40)
                    except Exception:
                        pass
                # Fire events so React registers the value
                try:
                    await elem.evaluate("""el => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""")
                except Exception:
                    pass
            except FormBlockedException:
                raise
            except Exception:
                pass

    return len(tasks)


# ---------------------------------------------------------------------------
# Submission success detection
# ---------------------------------------------------------------------------

SUCCESS_TEXT_PATTERNS = [
    "thank you for applying", "thank you for your application",
    "application submitted", "application received",
    "we've received your application", "we have received your application",
    "successfully submitted", "your application has been submitted",
    "your application was submitted", "thanks for applying",
    "thanks for your application", "your submission has been received",
    "application complete", "submission successful",
    "application confirmation", "you've applied", "you have applied",
    "applied on", "congratulations"
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

    # Check for login/registration walls
    try:
        for f in p_page.frames:
            if await f.locator("input[type='password']").count() > 0:
                return False
    except Exception:
        pass

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
                        if not any(kw in post_path.lower() for kw in ("login", "signin", "sign-in", "auth")):
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
                if any(kw in post_path.lower() for kw in ("login", "signin", "sign-in", "auth")):
                    pass
                elif not (post_path.endswith('/application') or post_path.endswith('/apply')):
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
    field_attempts = {}
    try:
        # Step 1: Navigated page captcha/login check
        block_status, block_reason = await detect_captcha_or_login_wall(page)
        if block_status:
            return block_status, block_reason

        p_page = _get_raw_playwright_page(page)

        # Early check for expired / closed / 404 job postings
        is_expired, expired_reason = await detect_expired_or_missing_job(p_page, initial_job_link=job_link)
        if is_expired:
            job_logger.error(f"Job is no longer open: {expired_reason}")
            return "Expired", f"Job posting expired / closed / 404: {expired_reason}"

        # Wait for any embedded ATS (e.g. Greenhouse/Lever on Webflow or custom domains)
        await wait_for_embedded_ats(p_page, job_logger)

        # Step 1.5: If we landed on a Job Description page instead of a form, click "Apply".
        # We don't rely on fields_count because Taleo and others have search bars that inflate the count.
        apply_selectors = [
            "#st-apply",
            "[data-sr-track='apply']",
            "[data-automation-id='applyButton']",
            "[data-automation-id='jobFoundationalApplyButton']",
            "[data-automation-id='adventureButton']",
            "[data-automation-id='applyManually']",
            "a:has-text('Apply Manually')",
            "button:has-text('Apply Manually')",
            "[data-automation-id='autofillWithResume']",
            "a:has-text('Autofill with Resume')",
            "button:has-text('Autofill with Resume')",
            "a:has-text(\"I'm interested\")",
            "button:has-text(\"I'm interested\")",
            "a:has-text('I’m interested')",
            "button:has-text('I’m interested')",
            "a:has-text('Im interested')",
            "button:has-text('Im interested')",
            "a:has-text('I am interested')",
            "button:has-text('I am interested')",
            "#ApplyOnline",
            "a[id*='ApplyOnline']",
            "[title='Apply Online']",
            "a[data-ph-at-id='apply-button']",
            "[data-ph-id*='apply']",
            "[data-testid*='apply-button']",
            "[data-qa*='apply']",
            "a[data-test='apply-button']",
            "button:has-text('Apply Now')",
            "button:has-text('Apply Online')",
            "a:has-text('Apply Now')",
            "a:has-text('Apply Online')",
            "button:has-text('Apply for this job')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply to job')",
            "a:has-text('Apply to job')",
            "button:has-text('Start Application')",
            "a:has-text('Start Application')",
            "input[value='Apply Online']",
            "input[value='Apply Now']",
            "input[value='Apply']",
            "a.btn-apply",
            "a.apply-job-btn",
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

                        # Capture any new tab that the Apply button opens (target="_blank" links).
                        # We track href, monitor context pages and popup event.
                        btn_href = None
                        try:
                            btn_href = await btn.get_attribute("href")
                        except Exception:
                            pass

                        _popup_page = None
                        def _on_popup(new_page):
                            nonlocal _popup_page
                            _popup_page = new_page
                        p_page.context.on("page", _on_popup)

                        old_pages = set(p_page.context.pages)
                        await btn.click(timeout=3000)
                        job_logger.info(f"Clicked initial 'Apply' button on job description page: {sel}")
                        clicked_initial_apply = True

                        # Wait briefly for popup or navigation to spawn
                        try:
                            await asyncio.sleep(2.0)
                        except Exception:
                            pass

                        try:
                            p_page.context.remove_listener("page", _on_popup)
                        except Exception:
                            pass

                        # If _popup_page wasn't caught via event, check newly opened pages in context
                        if _popup_page is None:
                            new_pages = [p for p in p_page.context.pages if p not in old_pages and not p.is_closed()]
                            if new_pages:
                                _popup_page = new_pages[-1]

                        if _popup_page is not None:
                            # A new tab opened — ensure it navigates away from about:blank
                            job_logger.info(f"Apply button opened a new tab: {_popup_page.url}")
                            if _popup_page.url == "about:blank" or not _popup_page.url.startswith("http"):
                                try:
                                    await _popup_page.wait_for_url(lambda u: u != "about:blank" and u.startswith("http"), timeout=6000)
                                except Exception:
                                    pass

                            # If it's STILL about:blank and we know the target href, navigate directly
                            if (_popup_page.url == "about:blank" or not _popup_page.url.startswith("http")) and btn_href and btn_href.startswith("http"):
                                job_logger.info(f"Tab stuck at about:blank; navigating directly to href: {btn_href}")
                                try:
                                    await _popup_page.goto(btn_href, timeout=30000, wait_until="domcontentloaded")
                                except Exception as ge:
                                    job_logger.warning(f"Direct navigation to {btn_href} failed: {ge}")

                            try:
                                await _popup_page.wait_for_load_state("domcontentloaded", timeout=10000)
                            except Exception:
                                pass

                            job_logger.info(f"Switching active page to new tab: {_popup_page.url}")
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

                        await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=8000)

                        # Handle intermediate modal like "Start Your Application" (Apply Manually / Autofill with Resume)
                        for _ in range(4):
                            clicked_modal = False
                            for f in p_page.frames:
                                for man_sel in [
                                    "[data-automation-id='applyManually']",
                                    "a:has-text('Apply Manually')",
                                    "button:has-text('Apply Manually')",
                                    "[data-automation-id='autofillWithResume']",
                                    "a:has-text('Autofill with Resume')",
                                    "button:has-text('Autofill with Resume')"
                                ]:
                                    try:
                                        man_btn = f.locator(man_sel).first
                                        if await man_btn.is_visible():
                                            await man_btn.click(timeout=3000)
                                            job_logger.info(f"Clicked intermediate modal action: {man_sel}")
                                            clicked_modal = True
                                            await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=5000)
                                            break
                                    except Exception:
                                        pass
                                if clicked_modal:
                                    break
                            if clicked_modal:
                                break
                            await asyncio.sleep(1)

                        break
                except Exception:
                    continue

        # Step 2: Loop to handle single or multi-page forms
        max_steps = 5
        frame = p_page.main_frame
        total_fields_filled_across_steps = 0
        resume_uploaded_in_form = False
        for step in range(1, max_steps + 1):
            job_logger.info(f"Processing Form Step {step}...")

            # Recheck CAPTCHA at each step
            block_status, block_reason = await detect_captcha_or_login_wall(page)
            if block_status:
                return block_status, block_reason

            await wait_for_fields_to_settle(p_page.main_frame)
            frame = await select_active_frame(p_page)

            # Scroll the form page fully to reveal any below-fold fields before scanning.
            # Some forms (e.g. Greenhouse on Convoso) place required fields (like
            # "Where are you currently based?") below the EEO section; if they are
            # off-screen, is_visible() returns False and we miss them entirely.
            try:
                await frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.3)
                await frame.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.2)
            except Exception:
                pass

            try:
                fields_found = await process_form_fields(frame, profile, job_logger, resume_already_uploaded=resume_uploaded_in_form, field_attempts=field_attempts)
                total_fields_filled_across_steps += fields_found
                resume_uploaded_in_form = True
            except FormBlockedException as fbe:
                job_logger.warning(f"Form execution halted: {fbe.status} - {fbe.reason}")
                return fbe.status, fbe.reason

            # Validation-error recovery loop: re-attempt filling up to twice more
            for recovery_pass in range(1, MAX_VALIDATION_RECOVERY_PASSES + 1):
                has_errors, reasons = await find_validation_problems(frame)
                if not has_errors:
                    break
                job_logger.warning(f"Validation issues detected (pass {recovery_pass}): {reasons}. Re-attempting fill...")
                resume_missing = any("file upload" in r.lower() or "resume" in r.lower() for r in reasons)
                try:
                    extra = await process_form_fields(
                        frame, profile, job_logger,
                        resume_already_uploaded=(resume_uploaded_in_form and not resume_missing),
                        field_attempts=field_attempts
                    )
                    total_fields_filled_across_steps += extra
                except FormBlockedException as fbe:
                    job_logger.warning(f"Form execution halted: {fbe.status} - {fbe.reason}")
                    return fbe.status, fbe.reason

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

                                late_href = None
                                try:
                                    late_href = await btn.get_attribute("href")
                                except Exception:
                                    pass

                                _late_popup = None
                                def _on_late_popup(new_page):
                                    nonlocal _late_popup
                                    _late_popup = new_page
                                p_page.context.on("page", _on_late_popup)

                                old_pages = set(p_page.context.pages)
                                await btn.click(timeout=3000)
                                job_logger.info(f"Clicked late 'Apply' button on page: {sel}")
                                clicked_late_apply = True

                                try:
                                    await asyncio.sleep(2.0)
                                except Exception:
                                    pass

                                try:
                                    p_page.context.remove_listener("page", _on_late_popup)
                                except Exception:
                                    pass

                                if _late_popup is None:
                                    new_pages = [p for p in p_page.context.pages if p not in old_pages and not p.is_closed()]
                                    if new_pages:
                                        _late_popup = new_pages[-1]

                                if _late_popup is not None:
                                    if _late_popup.url == "about:blank" or not _late_popup.url.startswith("http"):
                                        try:
                                            await _late_popup.wait_for_url(lambda u: u != "about:blank" and u.startswith("http"), timeout=6000)
                                        except Exception:
                                            pass

                                    if (_late_popup.url == "about:blank" or not _late_popup.url.startswith("http")) and late_href and late_href.startswith("http"):
                                        job_logger.info(f"Late apply tab stuck at about:blank; navigating directly to href: {late_href}")
                                        try:
                                            await _late_popup.goto(late_href, timeout=30000, wait_until="domcontentloaded")
                                        except Exception as ge:
                                            job_logger.warning(f"Direct navigation to {late_href} failed: {ge}")

                                    try:
                                        await _late_popup.wait_for_load_state("domcontentloaded", timeout=10000)
                                    except Exception:
                                        pass

                                    job_logger.info(f"Late apply opened tab ({_late_popup.url}); switching to it.")
                                    p_page = _late_popup
                                    if hasattr(page, '_page'):
                                        page._page = _late_popup
                                    elif hasattr(page, 'page'):
                                        page.page = _late_popup

                                await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=10000)
                                break
                        except Exception:
                            continue
                    if clicked_late_apply:
                        break

                if clicked_late_apply:
                    # Check immediately if clicking late apply opened a login/signup wall or captcha
                    block_status, block_reason = await detect_captcha_or_login_wall(page)
                    if block_status:
                        return block_status, block_reason

                    # Also check if late apply opened a new popup/tab in the browser context
                    try:
                        for cp in p_page.context.pages:
                            if cp != p_page and not cp.is_closed():
                                cp_count = await count_visible_candidate_fields(cp.main_frame)
                                if cp_count > 0:
                                    job_logger.info(f"Late apply opened a new active tab ({cp.url}); switching to it.")
                                    p_page = cp
                                    if hasattr(page, '_page'):
                                        page._page = cp
                                    elif hasattr(page, 'page'):
                                        page.page = cp
                                    break
                    except Exception:
                        pass

                    frame = await select_active_frame(p_page)
                    try:
                        extra = await process_form_fields(frame, profile, job_logger, field_attempts=field_attempts)
                        total_fields_filled_across_steps += extra
                    except FormBlockedException as fbe:
                        job_logger.warning(f"Form execution halted: {fbe.status} - {fbe.reason}")
                        return fbe.status, fbe.reason

                if total_fields_filled_across_steps == 0:
                    job_logger.warning("No form fields detected on Step 1. Waiting up to 60 seconds for a form to load or for manual intervention to open the form...")
                    for _ in range(30):
                        await asyncio.sleep(2)
                        
                        block_status, block_reason = await detect_captcha_or_login_wall(page)
                        if block_status:
                            job_logger.info(f"Detected {block_status} while waiting for form fields: {block_reason}")
                            return block_status, block_reason

                        # Check if a new tab opened during the wait
                        try:
                            for cp in p_page.context.pages:
                                if cp != p_page and not cp.is_closed():
                                    cp_count = await count_visible_candidate_fields(cp.main_frame)
                                    if cp_count > 0:
                                        job_logger.info(f"Detected new tab ({cp.url}) with form fields during wait; switching to it.")
                                        p_page = cp
                                        if hasattr(page, '_page'):
                                            page._page = cp
                                        elif hasattr(page, 'page'):
                                            page.page = cp
                                        break
                        except Exception:
                            pass
                            
                        frame = await select_active_frame(p_page)
                        try:
                            extra = await process_form_fields(frame, profile, job_logger, field_attempts=field_attempts)
                            if extra > 0:
                                total_fields_filled_across_steps += extra
                                job_logger.info("Form fields appeared! Resuming automation.")
                                break
                        except FormBlockedException as fbe:
                            job_logger.warning(f"Form execution halted: {fbe.status} - {fbe.reason}")
                            return fbe.status, fbe.reason

                    if total_fields_filled_across_steps == 0:
                        job_logger.error("Still no form fields detected after 60 seconds. Aborting.")
                        return "Expired", "Page not found or expired"

            # Check if there is a next step
            clicked_next = await find_and_click_next_button(frame, fields_found)
            if not clicked_next:
                # No next button found, assume we are on the final step
                job_logger.info("No next button found. Form filling complete.")
                break
            await wait_for_fields_to_settle(p_page.main_frame)

        # Step 3: Pre-submit validation
        block_status, block_reason = await detect_captcha_or_login_wall(page)
        if block_status:
            return block_status, block_reason

        frame = await select_active_frame(p_page)

        # Safety Check: If no fields were discovered or filled, do not attempt to click submit buttons
        if total_fields_filled_across_steps == 0:
            is_exp, exp_msg = await detect_expired_or_missing_job(p_page, initial_job_link=job_link)
            if is_exp:
                job_logger.error(f"Job is expired/closed: {exp_msg}")
                return "Expired", exp_msg
            job_logger.warning("No fillable application form fields were detected on the page.")
            return "Expired", "Page not found or expired"

        if dry_run:
            job_logger.info("Dry run enabled - form filled but stopping before the final submit click.")
            return "Dry Run", "Dry run mode - form filled but not submitted"

        # Step 4: Submission
        if config.AUTO_SUBMIT:
            # Pre-submit verification: verify every required field and document is filled before clicking submit
            for pre_pass in range(1, 4):
                has_problems, reasons = await find_validation_problems(frame)
                if not has_problems:
                    job_logger.info("Pre-submit verification passed: All required fields and documents are verified.")
                    break
                job_logger.warning(f"Pre-submit verification detected unfilled fields or errors (pass {pre_pass}): {reasons}. Filling before submit...")
                resume_missing = any("resume" in r.lower() or "file upload" in r.lower() for r in reasons)
                await process_form_fields(
                    frame, profile, job_logger,
                    resume_already_uploaded=(not resume_missing),
                    field_attempts=field_attempts
                )
                await wait_for_fields_to_settle(frame, timeout_ms=3000)

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
                await asyncio.sleep(5)

                submit_selectors = [
                    "spl-button:has-text('Submit')", "spl-button:has-text('Submit Application')",
                    "button[type='submit']", "input[type='submit']",
                    "#submit_app", "button[id*='submit']", "input[id*='submit']",
                    "button:has-text('Submit Application')", "button:has-text('Submit')",
                    "button:has-text('Send Application')", "button:has-text('Complete Application')",
                    "input[type='button'][value='Submit Application']", "input[type='button'][value='Submit']",
                    "button[data-qa*='submit']", "button[data-automation-id*='submit']",
                    "button[data-automation-id='bottom-navigation-submit-button']",
                    "button[data-testid*='submit']",
                    "button:has-text('Submit your application')",
                    "button:has-text('Submit Form')",
                    "input[type='button'][value='Submit your application']"
                ]

                submitted = False
                scopes_to_try = [frame, p_page.main_frame] + [f for f in p_page.frames if f not in (frame, p_page.main_frame) and not is_chat_frame(f)]
                for scope in scopes_to_try:
                    for selector in submit_selectors:
                        try:
                            btn = scope.locator(selector).first
                            if await btn.count() and await btn.is_visible() and await btn.is_enabled():
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
                    if submitted:
                        break

                # Fallback 1: Try role/button/anchor matches for 'Submit' text-based buttons
                if not submitted:
                    extra_text_selectors = [
                        "[role='button']:has-text('Submit Application')", "[role='button']:has-text('Submit')",
                        "a:has-text('Submit Application')", "a:has-text('Submit')",
                        "button:has-text('Apply Now'):not([id*='filter']):not([class*='ot-'])"
                    ]
                    try:
                        for scope in scopes_to_try:
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
            try{
                if(node.shadowRoot){
                    for(var s=0; s<node.shadowRoot.children.length; s++) visit(node.shadowRoot.children[s]);
                }
            }catch(e){}
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
                    block_status, block_reason = await detect_captcha_or_login_wall(page)
                    if block_status:
                        job_logger.warning(f"Submission blocked: {block_reason}")
                        return block_status, block_reason
                    
                    # Targeted recovery: only fill the specific fields that failed validation,
                    # scrolling each into view first (handles off-screen / below-fold fields).
                    has_errors, reasons = await find_validation_problems(frame)
                    if has_errors:
                        job_logger.warning(f"Validation errors appeared after submit (attempt {submit_attempt}): {reasons}. Attempting to fill missing fields.")
                        resume_missing = any("resume" in r.lower() or "file upload" in r.lower() for r in reasons)
                        await fill_aria_invalid_fields(frame, profile, job_logger, field_attempts=field_attempts)
                        await process_form_fields(
                            frame, profile, job_logger,
                            resume_already_uploaded=(not resume_missing),
                            field_attempts=field_attempts
                        )
                        continue  # Loop back and try submitting again

                    job_logger.warning("Submit button was clicked but no confirmation (URL change or success message) was detected.")
                    return "Failed", "No confirmation of submission detected after clicking submit"
            
            return "max retries reached", "Exceeded maximum submission attempts due to recurring validation errors."
        else:
            job_logger.info("Auto-submit is disabled. Skipping submission click.")
            return "Submitted", "Auto-submit disabled (manual review mode)"

    except FormBlockedException as fbe:
        job_logger.warning(f"Form filling blocked: {fbe.status} ({fbe.reason})")
        return fbe.status, fbe.reason
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        job_logger.error(f"Exception occurred during form filling: {e}\nTraceback:\n{tb}")
        return "Failed", f"Exception: {e}"
