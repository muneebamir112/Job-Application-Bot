import os
import re
import json
import difflib
import asyncio
from playwright.async_api import Page, ElementHandle
import config
from modules.logger import logger
from modules.ollama_client import query_ollama
from modules.captcha_detector import detect_captcha_or_login_wall

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
    "last_name": ["last name", "surname", "family name"],
    "full_name": ["full name", "your name", "candidate name", "applicant name", "legal name"],
    "email": ["email", "e-mail", "email address", "e-mail address"],
    "phone": ["phone", "mobile", "telephone", "phone number", "contact number"],
    "location": [
        "location", "city", "current city", "current location", "residence",
        "address", "where are you based", "based in"
    ],
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
    "skills": ["skills", "key skills", "technical skills", "list your skills"],
    "education": ["education", "highest degree", "degree", "qualification", "academic background"],
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
    if key == "current_company":
        work_experience = profile.get("work_experience")
        if isinstance(work_experience, list) and work_experience:
            company = work_experience[0].get("company") if isinstance(work_experience[0], dict) else None
            return str(company).strip() or None if company else None
        return None

    value = profile.get(key)
    if value is None:
        return None

    if isinstance(value, list):
        if key == "education":
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

    best_text = None
    best_score = 0.0
    for opt in options_texts:
        opt_norm = normalize_text(opt)
        if not opt_norm:
            continue
        if value_norm == opt_norm:
            return opt, 1.0
        score = difflib.SequenceMatcher(None, value_norm, opt_norm).ratio()
        if value_norm in opt_norm or opt_norm in value_norm:
            score = max(score, 0.85)
        # Token overlap catches cases plain sequence-similarity misses, e.g.
        # "Haltom City, TX" vs a rendered "Haltom City, Texas, United States" -
        # most of the words genuinely match even though "TX" != "Texas".
        value_tokens = set(value_norm.split())
        opt_tokens = set(opt_norm.split())
        if value_tokens:
            overlap_ratio = len(value_tokens & opt_tokens) / len(value_tokens)
            if overlap_ratio >= 0.66:
                score = max(score, 0.7 + 0.2 * overlap_ratio)
        if score > best_score:
            best_score = score
            best_text = opt
    return best_text, best_score


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


def strip_markdown_formatting(text: str) -> str:
    """Safety net in case Ollama ignores the plain-prose instruction: strips common markdown syntax."""
    if not text:
        return text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)  # __bold__
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)  # *italic*
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)  # # Headers
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)  # - bullet points
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)  # 1. numbered lists
    return text.strip()


async def ask_ollama_open_ended(label: str, profile: dict, job_logger, classification: str) -> str:
    """The only call site allowed to generate a free-text Ollama answer for a form field."""
    assert classification == "OPEN_ENDED", "Ollama may only produce free text for OPEN_ENDED fields"

    profile_context = json_context_string(profile)
    resume_text = (profile.get("resume_text") or "")[:4000]
    system_prompt = (
        "You are the candidate answering a job application question in first person. "
        "Use ONLY facts from the candidate profile and resume text provided. "
        "Be specific and concise (2-4 sentences). Do not invent facts that aren't present in the context. "
        "This answer is typed directly into a plain-text form field, so write in plain prose only: "
        "no markdown, no **bold**, no headers, no bullet points or numbered lists, no asterisks."
    )
    prompt = f"""
Candidate profile context:
{profile_context}

Relevant resume text:
{resume_text}

Question:
{label}

Answer the question professionally, concisely, and specifically, using only the facts above.
Write plain prose only - no markdown formatting of any kind:
"""
    job_logger.info(f"Open-ended field detected: '{label}'. Querying Ollama...")
    answer = query_ollama(prompt, system_prompt=system_prompt, timeout=config.OLLAMA_LONG_TIMEOUT)
    return strip_markdown_formatting(answer)


async def ask_ollama_choice(label: str, options_texts: list[str], profile: dict, job_logger, classification: str) -> str:
    """The only call site allowed to ask Ollama to pick among existing rendered options (never free text)."""
    assert classification == "CHOICE_FIELD", "This helper only selects among existing rendered options"

    profile_context = json_context_string(profile)
    system_prompt = "You are a helpful job application bot. Output ONLY the exact text of the option that best matches the question/context. Do not include markdown or explanations."
    prompt = f"""
Candidate Profile:
{profile_context}

Question:
{label}

Options:
{options_texts}

Choose the single best matching option. Your response MUST be exactly one of the options from the list above:
"""
    job_logger.info(f"No profile match for choice field '{label}'. Asking Ollama to pick among existing options...")
    return query_ollama(prompt, system_prompt=system_prompt)


# ---------------------------------------------------------------------------
# Frame-aware discovery
#
# Playwright's CSS selector engine already pierces open shadow roots for
# query_selector_all()/locator() calls, so shadow DOM needs no special
# handling here. Iframes are a separate document tree though, so the form
# may live in the top-level page OR in an embedded iframe. Rather than
# hardcoding any site's iframe naming/URL pattern, we pick whichever frame
# currently has the most visible fillable fields - a generic structural
# signal that works the same way for every site.
# ---------------------------------------------------------------------------

async def count_visible_candidate_fields(frame) -> int:
    try:
        elems = await frame.query_selector_all(GENERIC_FIELD_SELECTOR)
    except Exception:
        return 0
    count = 0
    for elem in elems:
        try:
            if await elem.is_visible():
                count += 1
        except Exception:
            continue
    return count


async def select_active_frame(p_page):
    """Returns the Page's main frame or whichever embedded iframe has the most visible fields."""
    best_frame = p_page.main_frame
    best_count = await count_visible_candidate_fields(best_frame)

    for frame in p_page.frames:
        if frame == p_page.main_frame:
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
    stops changing (two consecutive identical reads) or the timeout elapses,
    instead of a blind fixed sleep.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (timeout_ms / 1000)
    last_count = -1
    stable_hits = 0
    while loop.time() < deadline:
        count = await count_visible_candidate_fields(frame)
        if count == last_count:
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
]


async def find_visible_listbox_options(frame, wait_ms: int = LISTBOX_WAIT_MS) -> tuple[list[str], str | None]:
    """Polls briefly for a rendered suggestion/option list. Returns (option_texts, selector_used)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (wait_ms / 1000)
    while loop.time() < deadline:
        for selector in LISTBOX_OPTION_SELECTORS:
            try:
                locator = frame.locator(selector)
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
                    return texts, selector
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return [], None


async def click_option_by_text(frame, selector: str, text: str) -> bool:
    try:
        locator = frame.locator(selector).filter(has_text=text).first
        if await locator.is_visible():
            await locator.click()
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
    Universal autocomplete/combobox strategy, used for ANY field detected as a
    combobox regardless of which profile key (if any) it maps to - not just
    location. Focuses the field, types the matched profile value (if any),
    waits for options to render, clicks the best match, then verifies the
    committed value. When there's no profile value to type, it focuses/clicks
    first to see if a full option list renders without typing (many widgets
    behave like a styled <select>); if so, Ollama picks among only those
    rendered options - never free text.
    """
    value = get_profile_value(profile, matched_key) if matched_key else None

    try:
        await elem.scroll_into_view_if_needed()
        await elem.click()

        if value:
            await elem.evaluate("el => { if ('value' in el) el.value = ''; }")
            await elem.type(value, delay=40)

        option_texts, option_selector = await find_visible_listbox_options(frame)

        if option_texts:
            if value:
                best_text, score = best_matching_option(value, option_texts)
                source = "dropdown match"
            else:
                choice = await ask_ollama_choice(label, option_texts, profile, job_logger, "CHOICE_FIELD")
                normalized_best, _ = best_matching_option(choice, option_texts)
                best_text = normalized_best or choice
                score = 1.0
                source = "ollama"

            if best_text and score >= MATCH_THRESHOLD:
                clicked = await click_option_by_text(frame, option_selector, best_text)
                if clicked:
                    committed = await read_element_value(elem)
                    if committed and best_text.lower() not in committed.lower() and committed.lower() not in best_text.lower():
                        job_logger.warning(f"Combobox '{label}': committed value '{committed}' doesn't clearly match selected option '{best_text}'.")
                    log_field_decision(job_logger, label, "PROFILE_FIELD" if value else "CHOICE_FIELD", source, best_text)
                    return True

        if value:
            # No matching (or no rendered) option - plain text input, keep typed value
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

async def get_field_label(frame, element: ElementHandle) -> str:
    """
    Attempts to retrieve a human-readable label/placeholder/name for a given field
    element, checking label[for], aria-labelledby, aria-label, placeholder, name,
    and finally nearby visible text, in that priority order.
    """
    try:
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

        # Many component-based forms render the visible <label> as a sibling
        # of the field (inside a shared wrapper) rather than an ancestor, and
        # the label's `for` may target a wrapper id instead of the actual
        # input's id - neither label[for] nor closest('label') catches this.
        # Climb a few ancestor levels looking for a container that holds
        # EXACTLY one <label> (an unambiguous match); stop climbing as soon as
        # a container has more than one, since that means we've climbed past
        # this field's own boundary into a shared section with other fields.
        nearby_label_text = await element.evaluate("""el => {
            let node = el;
            for (let depth = 0; depth < 4 && node; depth++) {
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
            return '';
        }""")
        if nearby_label_text and nearby_label_text.strip():
            return nearby_label_text.strip()

        placeholder = await element.get_attribute("placeholder")
        if placeholder and placeholder.strip():
            return placeholder.strip()

        aria_label = await element.get_attribute("aria-label")
        if aria_label and aria_label.strip():
            return aria_label.strip()

        name_attr = await element.get_attribute("name")
        if name_attr and name_attr.strip():
            return name_attr.strip()

        nearby_text = await element.evaluate("el => { "
            "let parent = el.parentElement; "
            "if (!parent) return ''; "
            "return parent.innerText.split('\\n')[0];"
            "}")
        if nearby_text and nearby_text.strip():
            return nearby_text.strip()

    except Exception as e:
        logger.debug(f"Error getting field label: {e}")

    return ""


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


async def set_field_value(elem: ElementHandle, value: str) -> None:
    """Sets a value on a native input/textarea/select, or types into a contenteditable element."""
    is_editable = False
    try:
        is_editable = await elem.evaluate("el => el.isContentEditable === true")
    except Exception:
        pass

    await elem.click()
    await elem.press("Control+a")
    await elem.press("Backspace")
    if is_editable:
        await elem.type(value, delay=20)
    else:
        await elem.fill(value)


# ---------------------------------------------------------------------------
# Field fillers
# ---------------------------------------------------------------------------

async def fill_text_field(frame, elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Fills a text input, textarea, or contenteditable element."""
    # The validation-recovery loop re-scans the whole form; skip fields that
    # already have a value rather than re-filling (and, for OPEN_ENDED fields,
    # re-querying Ollama) all over again on every retry pass.
    existing_value = await read_element_value(elem)
    if existing_value:
        return True

    field_type = "combobox" if await is_combobox_element(elem) else "text"
    classification, matched_key = classify_field(label, field_type, profile)

    if classification == "CHOICE_FIELD":
        return await handle_combobox_field(frame, elem, label, profile, job_logger, matched_key)

    if matched_key == "location":
        return await handle_combobox_field(frame, elem, label, profile, job_logger, matched_key)

    if classification == "PROFILE_FIELD":
        value = get_profile_value(profile, matched_key)
        if not value:
            job_logger.warning(f"Profile field '{matched_key}' has no value for '{label}'. Skipping (Ollama not used for profile fields).")
            log_field_decision(job_logger, label, classification, "skipped-empty-profile-value", None)
            return False

        try:
            await elem.scroll_into_view_if_needed()
            await set_field_value(elem, value)
            log_field_decision(job_logger, label, classification, "profile.json", value)
            return True
        except Exception as e:
            job_logger.error(f"Failed to fill text field '{label}': {e}")
            return False

    # OPEN_ENDED - the only path allowed to reach Ollama for a free-text answer
    try:
        val = await ask_ollama_open_ended(label, profile, job_logger, classification)
    except Exception as e:
        job_logger.error(f"Ollama failed to answer open-ended question '{label}': {e}")
        return False

    try:
        await elem.scroll_into_view_if_needed()
        await set_field_value(elem, val)
        log_field_decision(job_logger, label, classification, "ollama", val)
        return True
    except Exception as e:
        job_logger.error(f"Failed to fill text field '{label}': {e}")
        return False


async def fill_select_field(elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Fills a native <select> dropdown by choosing an existing option."""
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
    if "disability" in label_norm:
        # US OFCCP/EEO self-identification survey - always decline (same
        # standardized wording across nearly every US job platform). This is
        # a sensitive personal disclosure, so it must never be an AI guess.
        for elem, opt_text in options:
            opt_norm = normalize_text(opt_text)
            if opt_norm.startswith("no") or "do not have a disability" in opt_norm or "don t have a disability" in opt_norm:
                await elem.scroll_into_view_if_needed()
                await elem.click()
                return True, "profile.json (disability status always declined)", opt_text

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

    if is_canada_work_authorization_question(label_norm):
        return True, "profile.json (Canada work-authorization treated as blanket Yes)"

    if is_canada_residency_question(label_norm):
        country = resolve_country_from_location(profile.get("location") or "")
        should_check = country in ("US", "CA")
        return should_check, f"profile.json (location-based: resolved country={country})"

    classification, matched_key = classify_field(label, "checkbox", profile)
    profile_value = get_profile_value(profile, matched_key)
    should_check = interpret_yes_no(profile_value)
    source = "profile.json"

    if should_check is None:
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

        if should_check:
            await elem.scroll_into_view_if_needed()
            await elem.check()

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
                const parent = btn.parentElement;
                if (!parent || seen.has(parent)) continue;
                const buttonChildren = Array.from(parent.children).filter(c => c.tagName === 'BUTTON');
                const texts = buttonChildren.map(c => (c.textContent || '').trim().toLowerCase());
                if (buttonChildren.length === 2 && texts.includes('yes') && texts.includes('no')) {
                    seen.add(parent);
                    results.push(parent);
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
        if any(term in label_lower for term in ["resume", "cv", "curriculum vitae", "cover letter"]):
            resume_path = profile.get("resume_file_path", "")
            if resume_path and os.path.exists(resume_path):
                absolute_path = os.path.abspath(resume_path)
                await elem.set_input_files(absolute_path)
                job_logger.info(f"Uploaded resume file '{absolute_path}' to field '{label}'")
                return True
            else:
                job_logger.error(f"Resume file path '{resume_path}' is invalid or file does not exist.")
        else:
            job_logger.info(f"Skipped file upload field '{label}' (not a resume/CV upload)")
            return True
    except Exception as e:
        job_logger.error(f"Failed to upload file to field '{label}': {e}")
    return False


async def find_and_click_next_button(frame) -> bool:
    """
    Searches for multi-step buttons like 'Next', 'Continue', 'Proceed', 'Step'
    and clicks them if found. Returns True if button was clicked.
    """
    next_selectors = [
        "button:has-text('Next')", "button:has-text('Continue')",
        "button:has-text('Proceed')", "input[type='button'][value='Next']",
        "input[type='button'][value='Continue']", "a:has-text('Next')",
        "button[id*='next']", "button[class*='next']"
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
                text = (await elem.inner_text()).strip()
                if text and len(text) < 200:
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


async def process_form_fields(frame, profile: dict, job_logger) -> bool:
    """
    Enumerates and fills out form fields in the current step, within a single
    frame (the main page or an embedded iframe - both expose the same
    query_selector_all/locator API in Playwright).
    """
    # 1. Inputs (Text, Email, Tel, etc.) and contenteditable text-like fields
    inputs = await frame.query_selector_all(
        "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='checkbox']):not([type='radio']):not([type='file'])"
    )
    for elem in inputs:
        if await elem.is_visible():
            label = await get_field_label(frame, elem)
            await fill_text_field(frame, elem, label, profile, job_logger)

    textareas = await frame.query_selector_all("textarea")
    for elem in textareas:
        if await elem.is_visible():
            label = await get_field_label(frame, elem)
            await fill_text_field(frame, elem, label, profile, job_logger)

    editable_divs = await frame.query_selector_all("[contenteditable='true']")
    for elem in editable_divs:
        if await elem.is_visible():
            label = await get_field_label(frame, elem)
            await fill_text_field(frame, elem, label, profile, job_logger)

    # 2. Native selects
    selects = await frame.query_selector_all("select")
    for elem in selects:
        if await elem.is_visible():
            label = await get_field_label(frame, elem)
            await fill_select_field(elem, label, profile, job_logger)

    # 3. Custom-widget comboboxes not already covered by the input loop above
    combobox_widgets = await frame.query_selector_all("[role='combobox']")
    for elem in combobox_widgets:
        try:
            tag = await elem.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            continue
        if tag in ("input", "textarea", "select"):
            continue
        if await elem.is_visible():
            label = await get_field_label(frame, elem)
            _, matched_key = classify_field(label, "combobox", profile)
            await handle_combobox_field(frame, elem, label, profile, job_logger, matched_key)

    # 4. File uploads (visibility not required - drag/drop dropzones hide the real input)
    files = await frame.query_selector_all("input[type='file']")
    for elem in files:
        label = await get_field_label(frame, elem)
        await handle_file_upload(elem, label, profile, job_logger)

    if files:
        # Some ATS forms (e.g. Ashby's "Autofill from resume") re-parse the
        # upload and re-render parts of the form afterward - give that a
        # moment to settle before touching anything else, so we don't grab
        # element handles that are about to be replaced.
        await wait_for_fields_to_settle(frame, timeout_ms=2000)

    # 5. Native radio buttons (grouped by name attribute to avoid duplicate Ollama calls)
    radios = await frame.query_selector_all("input[type='radio']")
    processed_radio_names = set()
    for elem in radios:
        if await elem.is_visible():
            name_attr = await elem.get_attribute("name")
            if name_attr and name_attr not in processed_radio_names:
                processed_radio_names.add(name_attr)
                label = await get_field_label(frame, elem)
                await fill_radio_group(frame, name_attr, label, profile, job_logger)

    # 6. ARIA custom-widget radio groups
    radiogroups = await frame.query_selector_all("[role='radiogroup']")
    for group in radiogroups:
        if await group.is_visible():
            group_label = (await group.get_attribute("aria-label")) or await get_field_label(frame, group)
            await fill_aria_radio_group(frame, group, group_label, profile, job_logger)

    # 7. Native checkboxes
    checkboxes = await frame.query_selector_all("input[type='checkbox']")
    for elem in checkboxes:
        if await elem.is_visible():
            label = await get_field_label(frame, elem)
            await fill_checkbox(elem, label, profile, job_logger)

    # 8. ARIA custom-widget checkboxes
    aria_checkboxes = await frame.query_selector_all("[role='checkbox']")
    for elem in aria_checkboxes:
        try:
            tag = await elem.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            continue
        if tag == "input":
            continue
        if await elem.is_visible():
            label = await get_field_label(frame, elem)
            await fill_aria_checkbox(elem, label, profile, job_logger)

    # 9. Yes/No button-pair boolean questions (e.g. work-authorization, residency
    # checks rendered as two styled <button>Yes</button>/<button>No</button>
    # elements instead of a native input - common beyond just one ATS)
    yesno_groups = await find_yesno_button_groups(frame, job_logger)
    job_logger.info(f"Detected {len(yesno_groups)} Yes/No button-pair field(s) on this step.")
    for container in yesno_groups:
        if await container.is_visible():
            label = await get_field_label(frame, container)
            await fill_yesno_buttons(frame, container, label, profile, job_logger)

    return True


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

    try:
        if p_page.url != pre_submit_url:
            return True
    except Exception:
        pass

    scopes = [p_page]
    if frame is not p_page.main_frame:
        scopes.append(frame)

    for scope in scopes:
        try:
            body_text = (await scope.inner_text("body")).lower()
            for pattern in SUCCESS_TEXT_PATTERNS:
                if pattern in body_text:
                    return True
        except Exception:
            continue

    return False


async def fill_and_submit_form(page: Page, profile: dict, job_logger, dry_run: bool = False) -> tuple[str, str]:
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

        # Step 2: Loop to handle single or multi-page forms
        max_steps = 5
        frame = p_page.main_frame
        for step in range(1, max_steps + 1):
            job_logger.info(f"Processing Form Step {step}...")

            # Recheck CAPTCHA at each step
            has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
            if has_captcha:
                return "Human Attention", captcha_reason

            await wait_for_fields_to_settle(p_page.main_frame)
            frame = await select_active_frame(p_page)

            await process_form_fields(frame, profile, job_logger)

            # Validation-error recovery loop: re-attempt filling up to twice more
            for recovery_pass in range(1, MAX_VALIDATION_RECOVERY_PASSES + 1):
                has_errors, reasons = await find_validation_problems(frame)
                if not has_errors:
                    break
                job_logger.warning(f"Validation issues detected (pass {recovery_pass}): {reasons}. Re-attempting fill...")
                await process_form_fields(frame, profile, job_logger)

            has_errors, reasons = await find_validation_problems(frame)
            if has_errors:
                return "Failed", f"Unresolved validation errors after retries: {reasons}"

            # Check if there is a next step
            clicked_next = await find_and_click_next_button(frame)
            if not clicked_next:
                # No next button found, assume we are on the final step
                job_logger.info("No next button found. Form filling complete.")
                break
            await wait_for_fields_to_settle(p_page.main_frame)

        # Step 3: Pre-submit validation and screenshot
        has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
        if has_captcha:
            return "Human Attention", captcha_reason

        frame = await select_active_frame(p_page)

        if config.SCREENSHOT_BEFORE_SUBMIT:
            screenshot_name = f"presubmit_{int(asyncio.get_event_loop().time())}.png"
            screenshot_path = os.path.join(config.LOGS_DIR, screenshot_name)
            await page.screenshot(path=screenshot_path)
            job_logger.info(f"Pre-submit screenshot saved to: {screenshot_path}")

        if dry_run:
            job_logger.info("Dry run enabled - form filled but stopping before the final submit click.")
            return "Dry Run", "Dry run mode - form filled but not submitted"

        # Step 4: Submission
        if config.AUTO_SUBMIT:
            submit_selectors = [
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Submit')", "button:has-text('Submit Application')",
                "button:has-text('Apply')", "input[type='button'][value='Submit']",
                "input[type='button'][value='Apply']"
            ]

            try:
                pre_submit_url = p_page.url
            except Exception:
                pre_submit_url = ""

            submitted = False
            for selector in submit_selectors:
                try:
                    btn = frame.locator(selector).first
                    if await btn.is_visible() and await btn.is_enabled():
                        await btn.scroll_into_view_if_needed()
                        await btn.click()
                        job_logger.info(f"Clicked submit button matching selector: '{selector}'")
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                return "Failed", "Could not locate visible submit button on the final page"

            # Wait for response/navigation - bounded settle-wait plus the existing
            # network-idle wait, instead of a blind fixed sleep
            try:
                await p_page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=5000)

            if await detect_submission_success(page, frame, pre_submit_url):
                job_logger.info("Application form submitted successfully (confirmation detected).")
                return "Submitted", ""
            else:
                job_logger.warning("Submit button was clicked but no confirmation (URL change or success message) was detected.")
                return "Failed", "No confirmation of submission detected after clicking submit"
        else:
            job_logger.info("Auto-submit is disabled. Skipping submission click.")
            return "Submitted", "Auto-submit disabled (manual review mode)"

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        job_logger.error(f"Exception occurred during form filling: {e}\nTraceback:\n{tb}")
        return "Failed", f"Exception: {e}"
