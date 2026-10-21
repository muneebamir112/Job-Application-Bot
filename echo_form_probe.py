import asyncio
import argparse
import sys
import os
import re
from datetime import datetime

# Windows console encoding safeguard
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# Import browser patch and browser-use
import modules.browser_use_patch  # noqa: F401
from browser_use import Browser, BrowserConfig
import config
from modules.logger import logger
from modules.form_filler import (
    get_field_label,
    select_active_frame,
    wait_for_fields_to_settle,
    wait_for_embedded_ats,
    is_chat_frame,
    is_chat_or_support_element,
    _get_raw_playwright_page
)

import gspread
from google.oauth2.service_account import Credentials

TARGET_SHEET_ID = "1FsPR9t-BB1GZ6kWfANnrDfq4p9XobDuA1tVB4D2sJDg"
TARGET_WORKSHEET_NAME = "responses"
SERVICE_ACCOUNT_JSON = os.path.join(config.PROJECT_ROOT, "service_account.json")

class ResponsesSheetLogger:
    """Manages appending captured form fields and echo responses to the 'responses' worksheet."""
    def __init__(self, sheet_id=TARGET_SHEET_ID, worksheet_name=TARGET_WORKSHEET_NAME):
        self.sheet_id = sheet_id
        self.worksheet_name = worksheet_name
        self.scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        self.client = self._authenticate()
        self.ws = self._open_or_create_worksheet()

    def _authenticate(self):
        try:
            creds = Credentials.from_service_account_file(
                SERVICE_ACCOUNT_JSON,
                scopes=self.scopes
            )
            return gspread.authorize(creds)
        except Exception as e:
            logger.error(f"[GoogleSheet] Authentication failed: {e}")
            raise

    def _open_or_create_worksheet(self):
        try:
            ss = self.client.open_by_key(self.sheet_id)
            for ws in ss.worksheets():
                if ws.title.strip().lower() == self.worksheet_name.strip().lower():
                    # Ensure headers exist
                    row1 = ws.row_values(1)
                    if not row1:
                        ws.append_row(["Timestamp", "Company", "Job Title", "Job URL", "Question / Field Label", "Field Type", "Answer / Value"])
                    return ws
            # If not found, create it
            ws = ss.add_worksheet(title=self.worksheet_name, rows="1000", cols="10")
            ws.append_row(["Timestamp", "Company", "Job Title", "Job URL", "Question / Field Label", "Field Type", "Answer / Value"])
            logger.info(f"[GoogleSheet] Created worksheet '{self.worksheet_name}'.")
            return ws
        except Exception as e:
            logger.error(f"[GoogleSheet] Failed to open/create worksheet '{self.worksheet_name}': {e}")
            return None

    def log_response(self, company: str, job_title: str, job_url: str, question: str, field_type: str, answer_value: str):
        """Immediately appends one row to the 'responses' worksheet."""
        if not self.ws:
            return
        clean_q = str(question or "").strip()
        clean_ans = str(answer_value or "").strip()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        row = [
            timestamp,
            str(company or "Unknown"),
            str(job_title or "Unknown"),
            str(job_url or ""),
            clean_q,
            str(field_type or "Unknown"),
            clean_ans
        ]

        for attempt in range(2):
            try:
                self.ws.append_row(row, value_input_option="USER_ENTERED")
                logger.info(f"[ResponsesSheet] Logged -> Question: '{clean_q}' | Type: {field_type} | Answer: '{clean_ans}'")
                return
            except Exception as e:
                logger.warning(f"[ResponsesSheet] Append row error (attempt {attempt+1}): {e}")
                try:
                    self.client = self._authenticate()
                    ss = self.client.open_by_key(self.sheet_id)
                    self.ws = ss.worksheet(self.worksheet_name)
                except Exception:
                    pass

def derive_echo_answer(question: str) -> str:
    """
    Derives the answer strictly based on the question prompt:
    - If question asks for email -> 'email'
    - If question asks for name -> 'name' or 'Name'
    - For any other question -> the question text itself!
    """
    if not question:
        return "answer"
    q_lower = question.lower()

    # If asking for email:
    if "email" in q_lower or "e-mail" in q_lower:
        return "email"

    # If asking for name (and not company name):
    if "company" not in q_lower:
        if "first name" in q_lower:
            return "First Name"
        elif "last name" in q_lower:
            return "Last Name"
        elif "full name" in q_lower or q_lower.strip().rstrip("?:* ").lower() == "name":
            return "Name"

    # For any other question, answer what the question is asked (stripping trailing punctuation):
    clean = re.sub(r'[\*\:]+$', '', question.strip()).strip()
    return clean if clean else question.strip()

async def dismiss_overlays(page):
    selectors = [
        "button:has-text('Accept All')",
        "button:has-text('Accept all cookies')",
        "button:has-text('Accept All Cookies')",
        "button:has-text('Accept all')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Allow all cookies')",
        "button:has-text('Allow All')",
        "button:has-text('I agree')",
        "button:has-text('I Agree')",
        "button:has-text('Got it')",
        "button:has-text('Dismiss')",
        "button[aria-label='Close']",
        "button.close"
    ]
    for s in selectors:
        try:
            for el in await page.locator(s).all():
                if await el.is_visible():
                    await el.click(timeout=1500)
                    await asyncio.sleep(0.5)
                    break
        except Exception:
            pass

async def click_apply_if_present(p_page) -> bool:
    """Clicks apply/start application button if landed on a job description page."""
    apply_selectors = [
        "#st-apply",
        "[data-sr-track='apply']",
        "[data-automation-id='applyButton']",
        "[data-automation-id='jobFoundationalApplyButton']",
        "[data-automation-id='applyManually']",
        "a:has-text('Apply Manually')",
        "button:has-text('Apply Manually')",
        "[data-automation-id='autofillWithResume']",
        "a:has-text('Apply Now'):not([id*='filter'])",
        "button:has-text('Apply Now'):not([id*='filter']):not(.dropdown-toggle)",
        "a:has-text('Apply for this job')",
        "button:has-text('Apply for this job')",
        "a:has-text('Apply Online')",
        "button:has-text('Apply Online')",
        "button:has-text('Start Application')",
        "a:has-text('Start Application')",
        "a:has-text('Apply')",
        "button:has-text('Apply')"
    ]
    for f in p_page.frames:
        if is_chat_frame(f):
            continue
        for sel in apply_selectors:
            try:
                btn = f.locator(sel).first
                if await btn.is_visible() and await btn.is_enabled():
                    logger.info(f"Clicking initial apply button: {sel}")
                    await btn.click(timeout=3000)
                    await asyncio.sleep(2)
                    return True
            except Exception:
                continue
    return False

async def process_form_step(frame, sheet_logger: ResponsesSheetLogger, company: str, job_title: str, job_url: str) -> int:
    """
    Scans the current frame/form:
    1. Fills text/textarea fields with the question/label text itself.
    2. Clicks every checkbox.
    3. Clicks every radio button.
    4. Handles dropdowns / select fields.
    5. Logs each action immediately to the Google Sheet 'responses' worksheet.
    """
    total_interacted = 0

    # 1. Process Checkboxes
    checkboxes = await frame.query_selector_all("input[type='checkbox'], [role='checkbox'], spl-checkbox")
    logger.info(f"Found {len(checkboxes)} checkbox elements.")
    for cb in checkboxes:
        try:
            if not await cb.is_visible() or await is_chat_or_support_element(cb):
                continue
            lbl = await get_field_label(frame, cb) or "Checkbox"
            
            # Click checkbox
            try:
                await cb.scroll_into_view_if_needed()
                await cb.click(timeout=2000)
            except Exception:
                await cb.evaluate("el => el.click()")

            total_interacted += 1
            sheet_logger.log_response(company, job_title, job_url, lbl, "Checkbox", "Clicked / Checked")
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug(f"Checkbox processing note: {e}")

    # 2. Process Radio Buttons
    radios = await frame.query_selector_all("input[type='radio'], [role='radio'], spl-radio")
    logger.info(f"Found {len(radios)} radio button elements.")
    for rb in radios:
        try:
            if not await rb.is_visible() or await is_chat_or_support_element(rb):
                continue
            lbl = await get_field_label(frame, rb, is_group=True) or "Radio Option"
            opt_text = ""
            try:
                opt_text = await rb.evaluate("""el => {
                    let p = el.parentElement;
                    return p ? p.innerText.trim() : (el.value || '');
                }""")
            except Exception:
                pass

            # Click radio button
            try:
                await rb.scroll_into_view_if_needed()
                await rb.click(timeout=2000)
            except Exception:
                await rb.evaluate("el => el.click()")

            total_interacted += 1
            full_val = f"Clicked ({opt_text})" if opt_text else "Clicked"
            sheet_logger.log_response(company, job_title, job_url, lbl, "Radio Button", full_val)
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug(f"Radio processing note: {e}")

    # 3. Process Text Inputs & Textareas
    text_fields = await frame.query_selector_all(
        "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='file']):not([type='radio']):not([type='checkbox']):not([type='image']), "
        "textarea, [contenteditable='true']"
    )
    logger.info(f"Found {len(text_fields)} text/textarea fields.")
    for tf in text_fields:
        try:
            if not await tf.is_visible() or await is_chat_or_support_element(tf):
                continue

            # Skip search boxes
            ph = (await tf.get_attribute("placeholder") or "").lower()
            tid = (await tf.get_attribute("data-testid") or "").lower()
            name_attr = (await tf.get_attribute("name") or "").lower()
            if any(k in ph or k in tid or k in name_attr for k in ("search", "filter")):
                continue

            # Retrieve Question / Label
            question = await get_field_label(frame, tf)
            if not question:
                question = await tf.get_attribute("placeholder") or await tf.get_attribute("name") or await tf.get_attribute("aria-label") or "Field"

            # Derive the answer echoing the question asked
            answer = derive_echo_answer(question)

            # Scroll into view and fill
            await tf.scroll_into_view_if_needed()
            
            # Disable HTML5 email/constraint blocking so literal 'email' doesn't fail native form validity
            await tf.evaluate("""el => {
                if (el.tagName === 'INPUT' && el.type === 'email') {
                    el.type = 'text';
                }
                if (el.form) {
                    el.form.noValidate = true;
                }
            }""")

            try:
                await tf.fill(answer, force=True)
            except Exception:
                await tf.focus()
                await tf.type(answer, delay=30)

            # Trigger standard input/change events for frontend frameworks
            await tf.evaluate("""el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""")

            total_interacted += 1
            sheet_logger.log_response(company, job_title, job_url, question, "Text Input", answer)
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug(f"Text field processing note: {e}")

    # 4. Process Select / Dropdowns
    selects = await frame.query_selector_all("select")
    for sel in selects:
        try:
            if not await sel.is_visible():
                continue
            lbl = await get_field_label(frame, sel) or "Dropdown"
            
            # Pick first option with actual value
            chosen_val = await sel.evaluate("""el => {
                for (let opt of el.options) {
                    if (opt.value && opt.value.trim() !== '') {
                        el.value = opt.value;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return opt.text || opt.value;
                    }
                }
                return '';
            }""")
            
            total_interacted += 1
            sheet_logger.log_response(company, job_title, job_url, lbl, "Dropdown", str(chosen_val or "Selected 1st option"))
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug(f"Select processing note: {e}")

    return total_interacted

async def click_next_or_submit(frame, submit: bool = False) -> str:
    """Attempts to click 'Next' / 'Continue' (or 'Submit' if submit=True). Returns action taken."""
    if submit:
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "button:has-text('Send Application')"
        ]
        for sel in submit_selectors:
            try:
                btn = frame.locator(sel).first
                if await btn.is_visible() and await btn.is_enabled():
                    await btn.click(timeout=3000)
                    logger.info(f"Clicked Submit button: {sel}")
                    return "Submitted"
            except Exception:
                pass

    next_selectors = [
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Save and Continue')",
        "a:has-text('Next')",
        "a:has-text('Continue')"
    ]
    for sel in next_selectors:
        try:
            btn = frame.locator(sel).first
            if await btn.is_visible() and await btn.is_enabled():
                await btn.click(timeout=3000)
                logger.info(f"Clicked Next/Continue button: {sel}")
                return "Next"
        except Exception:
            pass

    return "Done"

async def run_probe_for_job(page, job_url: str, company: str, job_title: str, sheet_logger: ResponsesSheetLogger, submit: bool = False):
    """Orchestrates navigating to a job application and running the form echo probe."""
    logger.info(f"--- Starting Probe on: {company} | {job_title} ---")
    logger.info(f"URL: {job_url}")

    p_page = _get_raw_playwright_page(page)
    try:
        await p_page.goto(job_url, timeout=45000, wait_until="domcontentloaded")
    except Exception as e:
        logger.warning(f"Initial navigation notice ({e}), proceeding with page state...")

    await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=6000)
    await dismiss_overlays(p_page)
    await wait_for_embedded_ats(p_page, logger)

    # If on job description page, click Apply
    await click_apply_if_present(p_page)
    await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=6000)

    # Multi-step loop
    total_interacted_all_steps = 0
    for step in range(1, 5):
        logger.info(f"Scanning & filling form step {step}...")
        frame = await select_active_frame(p_page)
        
        # Scroll down and back up to trigger lazy-loaded fields
        try:
            await frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.4)
            await frame.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.2)
        except Exception:
            pass

        interacted = await process_form_step(frame, sheet_logger, company, job_title, job_url)
        total_interacted_all_steps += interacted
        logger.info(f"Step {step}: Interacted with {interacted} form fields.")

        # Check for Next / Continue or Submit
        action = await click_next_or_submit(frame, submit=submit)
        if action == "Next":
            await wait_for_fields_to_settle(p_page.main_frame, timeout_ms=5000)
            continue
        elif action == "Submitted":
            logger.info("Form was submitted.")
            break
        else:
            logger.info("No further Next button detected; form processing finished.")
            break

    return total_interacted_all_steps

async def main():
    parser = argparse.ArgumentParser(description="Job Bot Form Echo Probe & Google Sheet Responses Logger")
    parser.add_argument("--url", type=str, default="", help="Single job link to test directly")
    parser.add_argument("--company", type=str, default="Target Company", help="Company name for single url test")
    parser.add_argument("--title", type=str, default="Target Role", help="Job title for single url test")
    parser.add_argument("--all", action="store_true", help="Process ALL links from Profile1 worksheet")
    parser.add_argument("--start-row", type=int, default=2, help="Row number in Profile1 to start from (default: 2)")
    parser.add_argument("--end-row", type=int, default=0, help="Row number in Profile1 to stop at (0 = end of sheet)")
    parser.add_argument("--profile", type=str, default="", help="Profile column to check for pending status (optional)")
    parser.add_argument("--pending", action="store_true", help="Process pending jobs from Profile1 worksheet")
    parser.add_argument("--limit", type=int, default=0, help="Max jobs to process (0 = process all)")
    parser.add_argument("--submit", action="store_true", help="Actually click the submit button after filling form (default is dry-run probe)")
    args = parser.parse_args()

    # 1. Initialize Google Sheet Logger
    logger.info("Connecting to Google Sheet ID: " + TARGET_SHEET_ID)
    sheet_logger = ResponsesSheetLogger(TARGET_SHEET_ID, TARGET_WORKSHEET_NAME)

    # 2. Setup stealth browser with isolated profile directory to avoid lock conflicts
    probe_profile_dir = os.path.join(config.PROJECT_ROOT, "scratch", "probe_browser_profile")
    os.makedirs(probe_profile_dir, exist_ok=True)

    b_config = BrowserConfig(
        headless=config.HEADLESS,
        disable_security=True,
        stealth=config.STEALTH_MODE,
        user_data_dir=probe_profile_dir
    )
    browser = Browser(config=b_config)
    context = await browser.new_context()

    try:
        if args.url:
            page = await context.get_current_page() or await context.new_tab()
            await run_probe_for_job(page, args.url, args.company, args.title, sheet_logger, submit=args.submit)
        else:
            # Read jobs from Google Sheet Profile1
            logger.info("Reading jobs from Profile1 worksheet...")
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=sheet_logger.scopes)
            client = gspread.authorize(creds)
            ss = client.open_by_key(TARGET_SHEET_ID)
            ws_profile = ss.worksheet("Profile1")
            all_rows = ws_profile.get_all_values()
            
            headers = [h.strip().lower() for h in all_rows[0]]
            link_idx = headers.index("job link") if "job link" in headers else 5
            comp_idx = headers.index("company name") if "company name" in headers else 1
            title_idx = headers.index("job title") if "job title" in headers else 2
            
            prof_col_idx = None
            if args.profile.strip():
                p_name = args.profile.strip().lower()
                for idx, h in enumerate(headers):
                    if p_name in h:
                        prof_col_idx = idx
                        break

            total_rows = len(all_rows)
            end_row = args.end_row if args.end_row > 0 else total_rows
            processed_count = 0

            logger.info(f"Scanning rows {args.start_row} to {end_row} (total sheet rows: {total_rows})...")

            for row_num in range(args.start_row, min(end_row + 1, total_rows + 1)):
                if args.limit > 0 and processed_count >= args.limit:
                    logger.info(f"Reached requested limit of {args.limit} jobs.")
                    break

                r = all_rows[row_num - 1]
                if len(r) <= link_idx:
                    continue
                j_link = r[link_idx].strip()
                if not j_link.startswith("http") or "chromewebdata" in j_link:
                    logger.warning(f"[Row {row_num}/{total_rows}] Skipping invalid/error link: '{j_link}'")
                    continue

                # If --profile was specified without --all, skip already processed
                if not args.all and prof_col_idx is not None and len(r) > prof_col_idx:
                    curr_status = r[prof_col_idx].strip()
                    if curr_status:
                        continue

                j_comp = r[comp_idx].strip() if len(r) > comp_idx else "Unknown"
                j_title = r[title_idx].strip() if len(r) > title_idx else "Unknown"

                logger.info(f"[{processed_count + 1}] Processing Row {row_num}/{total_rows}: {j_comp} - {j_title}")
                
                # Fresh page context for each job to ensure clean state
                page = await context.new_tab()
                try:
                    interacted = await run_probe_for_job(page, j_link, j_comp, j_title, sheet_logger, submit=args.submit)
                    logger.info(f"[Row {row_num}] Captured {interacted} fields for '{j_comp}'.")
                    processed_count += 1
                except Exception as e:
                    logger.warning(f"[Row {row_num}] Notice while probing {j_comp} ({e}). Continuing to next link...")
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

                await asyncio.sleep(1.5)

    finally:
        logger.info("Form probe completed. Closing browser context...")
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
