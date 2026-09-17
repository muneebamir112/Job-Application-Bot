import sys
import os
import asyncio

# Ensure Job-Bot is in python path
sys.path.insert(0, r"c:\Users\webNcodes\Desktop\webncodes\Job-Bot")

from modules.form_filler import (
    CHAT_IFRAME_PATTERNS,
    is_chat_frame,
    EXPIRED_JOB_PATTERNS,
    ALWAYS_YES_LABEL_KEYWORDS,
    _resolve_checkbox_state,
    normalize_text
)
from modules.captcha_detector import detect_captcha_or_login_wall

class MockFrame:
    def __init__(self, url="", name=""):
        self.url = url
        self.name = name

def test_chat_frame_detection():
    print("Testing chat frame detection...")
    chat_urls = [
        "https://careers.viasat.com/jibeapply/chat-widget.html",
        "https://widget.paradox.ai/widget/viasat",
        "https://js.intercomcdn.com/frame.html",
        "https://static.ada.support/embed2.html",
        "https://chat.drift.com/core/chat.html"
    ]
    for u in chat_urls:
        f = MockFrame(url=u)
        assert is_chat_frame(f), f"Expected chat frame for {u}"

    normal_urls = [
        "https://boards.greenhouse.io/embed/job_post?for=triafederal&token=5383455008",
        "https://jobs.ashbyhq.com/1password/f51cc73a-fde2-4686-b54b-f183cdaedb45",
        "https://jobs.lever.co/Trend-Health-Partners/form"
    ]
    for u in normal_urls:
        f = MockFrame(url=u)
        assert not is_chat_frame(f), f"Did not expect chat frame for {u}"
    print("[OK] Chat frame detection passed!")

def test_expired_patterns():
    print("Testing expired patterns against Viasat 404 HTML...")
    viasat_html_path = r"C:\Users\webNcodes\Desktop\Job Application Screenshots\Viasat\httpscareersviasatcomjobs6733langen-us\no_submit_page.html"
    if os.path.exists(viasat_html_path):
        with open(viasat_html_path, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read().lower()
        matched = [pat for pat in EXPIRED_JOB_PATTERNS if pat in html]
        assert len(matched) > 0, "Expected at least one expired pattern in Viasat 404 HTML"
        print(f"[OK] Viasat 404 correctly matched patterns: {matched}")
    else:
        print("Viasat HTML file not found on disk, skipping file test.")

async def test_checkbox_resolutions():
    print("Testing checkbox resolution...")
    profile = {
        "skills": ["TypeScript", "React", "Python", "Docker", "Kubernetes"],
        "work_authorization": "yes",
        "visa_sponsorship_needed": "no",
        "location": "Haltom City, TX"
    }

    # 1. Consents and agreements
    agreed, reason = await _resolve_checkbox_state("I agree to the Terms of Service and Privacy Policy", profile, None)
    assert agreed is True, f"Failed on agreement: {reason}"

    agreed2, reason2 = await _resolve_checkbox_state("By checking this box, I consent to the processing of my data", profile, None)
    assert agreed2 is True, f"Failed on consent: {reason2}"

    # 2. Skills matrix checkboxes
    skill_ts, reason_ts = await _resolve_checkbox_state("TypeScript", profile, None)
    assert skill_ts is True and "skills match" in reason_ts, f"Failed on TypeScript skill: {reason_ts}"

    skill_py, reason_py = await _resolve_checkbox_state("Python", profile, None)
    assert skill_py is True and "skills match" in reason_py, f"Failed on Python skill: {reason_py}"

    # 3. Always No checks
    conflict, reason_c = await _resolve_checkbox_state("Do you have a conflict of interest?", profile, None)
    assert conflict is False and "always no" in reason_c, f"Failed on conflict of interest: {reason_c}"

    print("[OK] Checkbox resolution tests passed!")

def test_numeric_parsing_and_field_detection():
    print("Testing numeric parsing and field detection...")
    from modules.form_filler import (
        parse_salary_value,
        parse_numeric_value,
        is_numeric_field,
        best_matching_option
    )

    # 1. Salary normalization
    assert parse_salary_value("130k") == "130000", "Failed on 130k"
    assert parse_salary_value("$130k") == "130000", "Failed on $130k"
    assert parse_salary_value("130,000") == "130000", "Failed on 130,000"
    assert parse_salary_value("$130,000") == "130000", "Failed on $130,000"
    assert parse_salary_value("120k - 140k", "beginning of desired annual salary") == "120000", "Failed on beginning range"
    assert parse_salary_value("120k - 140k", "end of desired annual salary") == "140000", "Failed on end range"

    # 2. Years experience and general numeric fields
    assert parse_numeric_value("8+ Years Experience", "Years of experience", matched_key="years_experience") == "8"
    assert parse_numeric_value("8+ Years Experience", "Total experience in years", elem_type="number") == "8"
    assert parse_numeric_value("130k", "What is the beginning of your desired annual base salary range? (please format with no commas, i.e. $50000, £50000)", matched_key="salary_expectation") == "130000"
    assert parse_numeric_value("May 2014", "Graduation year", matched_key="education_year") == "2014"

    # 3. Numeric field detection
    assert is_numeric_field("number", "", "", "Any label") is True
    assert is_numeric_field("text", "numeric", "", "Any label") is True
    assert is_numeric_field("text", "", "", "What is the beginning of your desired annual base salary range? (please format with no commas, i.e. $50000, £50000)") is True
    assert is_numeric_field("text", "", "", "Years of experience") is True
    assert is_numeric_field("text", "", "", "First Name") is False

    # 4. Country alias & fuzzy matching
    best_text, score = best_matching_option("US", ["Australia +61", "United States +1"])
    assert best_text == "United States +1" and score >= 0.9, f"Expected United States +1 for US, got {best_text} (score {score})"

    best_text_ca, score_ca = best_matching_option("CA", ["Cambodia +855", "Canada +1"])
    assert best_text_ca == "Canada +1" and score_ca >= 0.9, f"Expected Canada +1 for CA, got {best_text_ca} (score {score_ca})"

    print("[OK] Numeric parsing and fuzzy matching tests passed!")

async def main():
    test_chat_frame_detection()
    test_expired_patterns()
    await test_checkbox_resolutions()
    test_numeric_parsing_and_field_detection()
    print("\nALL generic improvement unit tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
