import os
import re
import asyncio
from playwright.async_api import Page, ElementHandle
import config
from modules.logger import logger
from modules.ollama_client import query_ollama
from modules.captcha_detector import detect_captcha_or_login_wall

async def get_field_label(page: Page, element: ElementHandle) -> str:
    """
    Attempts to retrieve a human-readable label/placeholder/name for a given field element.
    """
    try:
        # 1. Check ID-based label
        elem_id = await element.get_attribute("id")
        if elem_id:
            label_elem = await page.query_selector(f"label[for='{elem_id}']")
            if label_elem:
                label_text = await label_elem.inner_text()
                if label_text.strip():
                    return label_text.strip()
        
        # 2. Check parent label
        parent_label = await element.evaluate_handle("el => el.closest('label')")
        if parent_label and await parent_label.as_element():
            label_text = await parent_label.as_element().inner_text()
            if label_text.strip():
                return label_text.strip()
        
        # 3. Check placeholder
        placeholder = await element.get_attribute("placeholder")
        if placeholder and placeholder.strip():
            return placeholder.strip()
            
        # 4. Check aria-label or name
        aria_label = await element.get_attribute("aria-label")
        if aria_label and aria_label.strip():
            return aria_label.strip()
            
        name_attr = await element.get_attribute("name")
        if name_attr and name_attr.strip():
            return name_attr.strip()
            
        # 5. Fallback to nearby text
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

def fuzzy_match_profile(label: str, profile: dict) -> tuple[str, str]:
    """
    Fuzzy-matches a label against profile fields.
    Returns (matched_key, value) if found, otherwise (None, None).
    """
    label_lower = label.lower()
    
    mapping = {
        "first_name": ["first name", "given name", "first"],
        "last_name": ["last name", "surname", "family name", "last"],
        "full_name": ["full name", "name", "your name", "candidate name"],
        "email": ["email", "e-mail", "email address", "e-mail address"],
        "phone": ["phone", "mobile", "telephone", "phone number", "contact number"],
        "location": ["location", "city", "address", "current city", "residence"],
        "linkedin": ["linkedin", "linked in"],
        "github": ["github", "git hub"],
        "portfolio": ["portfolio", "website", "personal website", "blog"],
        "current_title": ["current title", "headline", "job title", "role"],
        "years_experience": ["years of experience", "experience level", "experience years", "years exp"],
        "work_authorization": ["work authorization", "authorized to work", "legal authorization", "right to work"],
        "visa_sponsorship_needed": ["visa", "sponsorship", "sponsor", "require visa"],
        "salary_expectation": ["salary", "compensation", "expected salary", "salary expectation"],
        "notice_period": ["notice period", "notice", "availability", "start date"],
        "willing_to_relocate": ["relocate", "relocation", "willing to relocate"]
    }
    
    for key, keywords in mapping.items():
        for kw in keywords:
            # Check if keyword is in the label or label is in keyword
            if kw in label_lower or label_lower in kw:
                val = profile.get(key, "")
                if val:
                    return key, str(val)
                    
    return None, None

async def fill_text_field(page: Page, elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Fills in a text or textarea input field."""
    matched_key, val = fuzzy_match_profile(label, profile)
    source = "profile"
    
    if not val:
        # If no profile match, use Ollama
        source = "ollama"
        job_logger.info(f"Open-ended field detected: '{label}'. Querying Ollama...")
        
        # Build context
        profile_context = json_context_string(profile)
        system_prompt = "You are a professional job applicant. Answer the question using the candidate's profile context. Be concise and professional (2-4 sentences)."
        prompt = f"""
Candidate profile context:
{profile_context}

Question:
{label}

Answer the question professionally and concisely based on the context:
"""
        try:
            val = query_ollama(prompt, system_prompt=system_prompt)
        except Exception as e:
            job_logger.error(f"Ollama failed to answer open-ended question '{label}': {e}")
            return False

    try:
        await elem.scroll_into_view_if_needed()
        await elem.click()
        # Select all and delete before typing
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await elem.fill(val)
        job_logger.info(f"Filled text field '{label}' with: '{val}' (Source: {source})")
        return True
    except Exception as e:
        job_logger.error(f"Failed to fill text field '{label}': {e}")
        return False

async def fill_select_field(elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Fills a select / dropdown field by choosing the best option using Ollama or matching."""
    try:
        await elem.scroll_into_view_if_needed()
        options_data = await elem.evaluate("el => Array.from(el.options).map(o => ({text: o.text, value: o.value}))")
        options_texts = [opt["text"].strip() for opt in options_data if opt["text"].strip()]
        
        if not options_texts:
            return False
            
        matched_key, val = fuzzy_match_profile(label, profile)
        
        # Determine selection via Ollama
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
        selected_option_text = query_ollama(prompt, system_prompt=system_prompt)
        
        # Find option matching the chosen text
        matching_value = None
        for opt in options_data:
            if opt["text"].strip().lower() == selected_option_text.strip().lower() or selected_option_text.strip().lower() in opt["text"].strip().lower():
                matching_value = opt["value"]
                selected_option_text = opt["text"]
                break
                
        if matching_value is None:
            # Fallback to first option if no match
            matching_value = options_data[0]["value"]
            selected_option_text = options_data[0]["text"]
            
        await elem.select_option(value=matching_value)
        job_logger.info(f"Selected option '{selected_option_text}' for dropdown '{label}' (Source: ollama)")
        return True
    except Exception as e:
        job_logger.error(f"Failed to fill select field '{label}': {e}")
        return False

async def fill_radio_group(page: Page, name_attr: str, label: str, profile: dict, job_logger) -> bool:
    """Fills a group of radio buttons with the same name attribute."""
    try:
        radios = await page.query_selector_all(f"input[type='radio'][name='{name_attr}']")
        radio_options = []
        for r in radios:
            # Get label associated with this radio
            r_id = await r.get_attribute("id")
            r_label = ""
            if r_id:
                l_elem = await page.query_selector(f"label[for='{r_id}']")
                if l_elem:
                    r_label = await l_elem.inner_text()
            if not r_label.strip():
                # Try sibling text
                r_label = await r.evaluate("el => el.parentElement.innerText")
            radio_options.append((r, r_label.strip()))
            
        options_texts = [item[1] for item in radio_options if item[1]]
        if not options_texts:
            return False

        # Query Ollama to select
        profile_context = json_context_string(profile)
        system_prompt = "You are a helpful job application bot. Output ONLY the exact text of the option that best matches. No extra chars."
        prompt = f"""
Candidate Profile:
{profile_context}

Question/Field label:
{label}

Options:
{options_texts}

Choose the single best matching option. Your response MUST be exactly one of the options from the list above:
"""
        selected_option_text = query_ollama(prompt, system_prompt=system_prompt)
        
        # Click the matching radio
        for r_elem, opt_text in radio_options:
            if opt_text.lower() == selected_option_text.lower() or selected_option_text.lower() in opt_text.lower():
                await r_elem.scroll_into_view_if_needed()
                await r_elem.click()
                job_logger.info(f"Selected radio '{opt_text}' for group '{label}' (Source: ollama)")
                return True
                
        # Fallback click first radio
        await radio_options[0][0].click()
        job_logger.info(f"Selected fallback radio '{radio_options[0][1]}' for group '{label}'")
        return True
    except Exception as e:
        job_logger.error(f"Failed to fill radio group '{label}': {e}")
        return False

async def fill_checkbox(elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Handles checking checkboxes (e.g. Terms, equal opportunity, etc.)."""
    try:
        label_lower = label.lower()
        # Usually we want to agree to terms/agreements or optional disclosures
        # Let's verify via Ollama if we should check it
        profile_context = json_context_string(profile)
        system_prompt = "You are a job application bot. Return 'yes' if the checkbox should be checked or 'no' if not. Be concise."
        prompt = f"""
Candidate profile details:
{profile_context}

Checkbox Label:
{label}

Should the candidate check/agree to this checkbox? (Answer 'yes' or 'no' only):
"""
        answer = query_ollama(prompt, system_prompt=system_prompt).lower()
        if "yes" in answer:
            await elem.scroll_into_view_if_needed()
            await elem.check()
            job_logger.info(f"Checked checkbox '{label}'")
        return True
    except Exception as e:
        job_logger.error(f"Failed to check checkbox '{label}': {e}")
        return False

async def handle_file_upload(elem: ElementHandle, label: str, profile: dict, job_logger) -> bool:
    """Uploads the resume file if the field is for resume/CV."""
    try:
        label_lower = label.lower()
        if any(term in label_lower for term in ["resume", "cv", "curriculum vitae", "cover letter"]):
            resume_path = profile.get("resume_file_path", "")
            if resume_path and os.path.exists(resume_path):
                absolute_path = os.path.abspath(resume_path)
                await elem.scroll_into_view_if_needed()
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

def json_context_string(profile: dict) -> str:
    """Formats the profile details cleanly for Ollama context."""
    # Exclude list objects or redundant paths for clarity
    filtered_profile = {k: v for k, v in profile.items() if k not in ["resume_file_path"]}
    return json.dumps(filtered_profile, indent=2)

async def find_and_click_next_button(page: Page) -> bool:
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
            btn = page.locator(selector).first
            if await btn.is_visible() and await btn.is_enabled():
                # Make sure it's not a submit button (unless it's the last page)
                btn_type = await btn.get_attribute("type")
                btn_text = await btn.inner_text()
                if btn_type == "submit" and any(term in btn_text.lower() for term in ["submit", "apply", "finish"]):
                    continue # Let the final submit block handle it
                
                await btn.scroll_into_view_if_needed()
                await btn.click()
                logger.info(f"Clicked next step button: '{btn_text or selector}'")
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2) # Give a second for step transition
                return True
        except Exception:
            continue
    return False

async def process_form_fields(page: Page, profile: dict, job_logger) -> bool:
    """
    Enumerates and fills out form fields in the current page step.
    """
    # 1. Inputs (Text, Email, Tel, etc.)
    inputs = await page.query_selector_all("input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='checkbox']):not([type='radio']):not([type='file'])")
    for elem in inputs:
        if await elem.is_visible():
            label = await get_field_label(page, elem)
            await fill_text_field(page, elem, label, profile, job_logger)
            
    # 2. Textareas
    textareas = await page.query_selector_all("textarea")
    for elem in textareas:
        if await elem.is_visible():
            label = await get_field_label(page, elem)
            await fill_text_field(page, elem, label, profile, job_logger)

    # 3. Selects
    selects = await page.query_selector_all("select")
    for elem in selects:
        if await elem.is_visible():
            label = await get_field_label(page, elem)
            await fill_select_field(elem, label, profile, job_logger)

    # 4. File Uploads
    files = await page.query_selector_all("input[type='file']")
    for elem in files:
        if await elem.is_visible():
            label = await get_field_label(page, elem)
            await handle_file_upload(elem, label, profile, job_logger)

    # 5. Radio Buttons (grouped by name attribute to avoid duplicate Ollama calls)
    radios = await page.query_selector_all("input[type='radio']")
    processed_radio_names = set()
    for elem in radios:
        if await elem.is_visible():
            name_attr = await elem.get_attribute("name")
            if name_attr and name_attr not in processed_radio_names:
                processed_radio_names.add(name_attr)
                label = await get_field_label(page, elem)
                await fill_radio_group(page, name_attr, label, profile, job_logger)

    # 6. Checkboxes
    checkboxes = await page.query_selector_all("input[type='checkbox']")
    for elem in checkboxes:
        if await elem.is_visible():
            label = await get_field_label(page, elem)
            await fill_checkbox(elem, label, profile, job_logger)

    return True

async def fill_and_submit_form(page: Page, profile: dict, job_logger) -> tuple[str, str]:
    """
    Main orchestration loop for navigating and filling a single job application.
    Returns (status, reason).
    """
    try:
        # Step 1: Navigated page captcha/login check
        has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
        if has_captcha:
            return "Human Attention", captcha_reason

        # Step 2: Loop to handle single or multi-page forms
        max_steps = 5
        for step in range(1, max_steps + 1):
            job_logger.info(f"Processing Form Step {step}...")
            
            # Recheck CAPTCHA at each step
            has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
            if has_captcha:
                return "Human Attention", captcha_reason
                
            await process_form_fields(page, profile, job_logger)
            
            # Check if there is a next step
            clicked_next = await find_and_click_next_button(page)
            if not clicked_next:
                # No next button found, assume we are on the final step
                job_logger.info("No next button found. Form filling complete.")
                break

        # Step 3: Pre-submit validation and screenshot
        has_captcha, captcha_reason = await detect_captcha_or_login_wall(page)
        if has_captcha:
            return "Human Attention", captcha_reason

        if config.SCREENSHOT_BEFORE_SUBMIT:
            screenshot_name = f"presubmit_{int(asyncio.get_event_loop().time())}.png"
            screenshot_path = os.path.join(config.LOGS_DIR, screenshot_name)
            await page.screenshot(path=screenshot_path)
            job_logger.info(f"Pre-submit screenshot saved to: {screenshot_path}")

        # Step 4: Submission
        if config.AUTO_SUBMIT:
            submit_selectors = [
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Submit')", "button:has-text('Submit Application')",
                "button:has-text('Apply')", "input[type='button'][value='Submit']",
                "input[type='button'][value='Apply']"
            ]
            
            submitted = False
            for selector in submit_selectors:
                try:
                    btn = page.locator(selector).first
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

            # Wait for response/navigation
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(5) # Let success page load
            job_logger.info("Application form submitted successfully.")
            return "Submitted", ""
        else:
            job_logger.info("Auto-submit is disabled. Skipping submission click.")
            return "Submitted", "Auto-submit disabled (manual review mode)"
            
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        job_logger.error(f"Exception occurred during form filling: {e}\nTraceback:\n{tb}")
        return "Failed", f"Exception: {e}"
