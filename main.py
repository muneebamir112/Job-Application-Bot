import asyncio
import argparse
from datetime import datetime
import sys
import os

# Windows console (cp1252) can't encode the emoji browser-use's internal
# loggers emit; without this, every such log line raises UnicodeEncodeError
# and spams "--- Logging error ---" blocks. Replace unencodable characters
# instead of crashing the handler.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import modules.browser_use_patch  # noqa: F401 - must run before Browser/BrowserConfig are used
from browser_use import Browser, BrowserConfig
import config
from modules.logger import logger, log_run_summary, get_job_logger
from modules.resume_parser import get_or_create_profile
from modules.sheet_sync import SheetSync
from modules.form_filler import fill_and_submit_form, wait_for_fields_to_settle

async def dismiss_overlays(page, logger):
    """Attempt to find and click common cookie consent / overlay dismiss buttons."""
    selectors = [
        "button:has-text('Accept All')",
        "button:has-text('I Agree')",
        "button:has-text('Allow Cookies')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Got it')",
        "button:has-text('Accept')",
        "a:has-text('Accept')",
        "a:has-text('I Agree')"
    ]
    
    for selector in selectors:
        try:
            # Quick check if any matching element is visible
            elements = await page.locator(selector).all()
            for el in elements:
                if await el.is_visible():
                    logger.info(f"Dismissing overlay using selector: {selector}")
                    await el.click(timeout=3000)
                    import asyncio
                    await asyncio.sleep(1) # wait a moment for animation to finish
        except Exception:
            pass # ignore errors if elements don't exist or become detached

async def run_bot(retry_failed: bool, retry_human_attention: bool, dry_run: bool = False, num_profiles: int = 0):
    logger.info("Initializing Job Application Automation Bot...")
    run_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Step 1: Discover and load profiles
    profiles_dir = os.path.join(config.PROJECT_ROOT, "profiles")
    all_profiles = {}
    if not os.path.exists(profiles_dir):
        logger.error(f"Profiles directory not found at {profiles_dir}. Please create it and add profile JSONs.")
        sys.exit(1)
        
    import glob
    import json
    for p_file in glob.glob(os.path.join(profiles_dir, "*.json")):
        try:
            with open(p_file, "r", encoding="utf-8") as f:
                p_data = json.load(f)
                if "name" in p_data or "full_name" in p_data:
                    p_name = p_data.get("name") or p_data.get("full_name")
                    all_profiles[p_name] = p_data
        except Exception as e:
            logger.error(f"Failed to load profile {p_file}: {e}")

    if not all_profiles:
        logger.error("No valid profiles found in profiles directory. Please add profile JSONs.")
        sys.exit(1)
    
    logger.info(f"Loaded {len(all_profiles)} profiles: {list(all_profiles.keys())}")
    
    if num_profiles > 0:
        limited_keys = list(all_profiles.keys())[:num_profiles]
        all_profiles = {k: all_profiles[k] for k in limited_keys}
        logger.info(f"Limiting to {num_profiles} profile(s): {list(all_profiles.keys())}")

    # Step 2: Initialize Google Sheet
    try:
        sheet = SheetSync()
        pending_jobs = sheet.get_pending_jobs(
            retry_failed=retry_failed,
            retry_human_attention=retry_human_attention,
            profiles_to_check=list(all_profiles.keys())
        )
    except Exception as e:
        logger.error(f"Failed to load Google Sheet: {e}")
        sys.exit(1)

    if not pending_jobs:
        logger.info("No pending jobs to process.")
        log_run_summary(0, 0, 0)
        return

    logger.info(f"Found {len(pending_jobs)} job(s) to process.")

    # Step 3: Launch visible browser via browser-use
    browser_config = BrowserConfig(
        headless=config.HEADLESS,
        disable_security=True,
        stealth=config.STEALTH_MODE
    )
    browser = Browser(config=browser_config)
    
    submitted_count = 0
    failed_count = 0
    human_attention_count = 0

    try:
        for job in pending_jobs:
            row_idx = job["row_index"]
            company = job["company"]
            title = job["title"]
            link = job["link"]

            profiles_to_apply = job.get("profiles_to_apply", {})
            if not profiles_to_apply:
                continue

            for profile_name, col_idx in profiles_to_apply.items():
                profile = all_profiles.get(profile_name)
                if not profile:
                    logger.warning(f"Profile {profile_name} found in sheet but missing JSON data, skipping.")
                    continue

                logger.info(f"Processing job {row_idx}: {title} at {company} (Profile: {profile_name})...")
                job_logger, log_path = get_job_logger(company, title, run_timestamp)
                job_logger.info(f"Starting application: {title} at {company} for {profile_name}")
                job_logger.info(f"URL: {link}")

                company_resume_path = os.path.join(config.CVS_DIR, company, f"{profile_name}.pdf")
                if not os.path.exists(company_resume_path):
                    job_logger.warning(
                        f"No tailored resume found for '{company}' at {company_resume_path} "
                        f"(run Generate Resumes first) - skipping until one exists."
                    )
                    logger.warning(f"Job {row_idx} skipped for {profile_name}: no tailored resume for '{company}'.")
                    logger.info(f"Finished job {row_idx} processing for {profile_name}. Log saved to {log_path}")
                    continue
                
                profile["resume_file_path"] = company_resume_path
                try:
                    from modules.resume_parser import extract_text_from_pdf
                    profile["resume_text"] = extract_text_from_pdf(company_resume_path)
                except Exception as e:
                    job_logger.error(f"Failed to extract text from tailored resume: {e}")
                    
                profile["job_title"] = title
                profile["company_name"] = company

                # Create a fresh context and page for each application to avoid browser crashes cascading
                context = await browser.new_context()
                try:
                    page = await context.get_current_page()
                    # If page is still None, we might need new_tab() based on the error hint
                    if not page:
                        page = await context.new_tab()
                
                    # Navigate to the link
                    job_logger.info(f"Navigating to: {link}")
                    # Use standard playwright page under browser-use context
                    p_page = page
                    if hasattr(page, 'page'):
                        p_page = page.page
                    elif hasattr(page, 'get_playwright_page'):
                        p_page = page.get_playwright_page()
                    elif hasattr(page, '_page'):
                        p_page = page._page

                    await p_page.goto(link, timeout=60000, wait_until="load")
                    await wait_for_fields_to_settle(p_page.main_frame) # Bounded wait for dynamic assets

                    # Dismiss cookie banners and overlays before filling form
                    await dismiss_overlays(p_page, job_logger)

                    # Fill and Submit form
                    status, reason = await fill_and_submit_form(page, profile, job_logger, company, link, dry_run=dry_run)

                    # Log outcome
                    if status == "Submitted":
                        submitted_count += 1
                        job_logger.info("Application submitted successfully!")
                    elif status == "Human Attention":
                        human_attention_count += 1
                        job_logger.warning(f"Requires Human Attention: {reason}")
                    elif status == "Dry Run":
                        job_logger.info(f"Dry run complete: {reason}")
                    else:
                        failed_count += 1
                        job_logger.error(f"Application failed: {reason}")

                    # Update live Sheet (dry runs are a local preview only - never touch the sheet)
                    if not dry_run and status != "Dry Run":
                        sheet.update_profile_status(row_idx, col_idx, f"Generated | {status}")
                        # Log Ollama Q&A to Sheet3
                        ollama_answers = getattr(job_logger, "ollama_answers", [])
                        sheet.log_form_answers(profile_name, ollama_answers)

                except Exception as e:
                    failed_count += 1
                    import traceback
                    tb = traceback.format_exc()
                    job_logger.error(f"Unexpected exception during processing: {e}\n{tb}")
                    logger.error(f"Job {row_idx} failed for {profile_name} with unexpected exception: {e}")
                    if not dry_run:
                        sheet.update_profile_status(row_idx, col_idx, "Generated | Failed")

                finally:
                    await context.close()
                    logger.info(f"Finished job {row_idx} processing for {profile_name}. Log saved to {log_path}")

    finally:
        # Close browser session cleanly
        await browser.close()

    # Log overall run summary
    log_run_summary(submitted_count, failed_count, human_attention_count)

def main():
    parser = argparse.ArgumentParser(description="Job Application Automation Bot")
    parser.add_argument("--retry-failed", action="store_true", help="Retry applications with 'Failed' status")
    parser.add_argument("--retry-human-attention", action="store_true", help="Retry applications with 'Human Attention' status")
    parser.add_argument("--dry-run", action="store_true", help="Fill out forms but stop before the final submit click; never updates the sheet")
    parser.add_argument("--num-profiles", type=int, default=0, help="Number of profiles to apply for. If 0, apply for all profiles.")
    args = parser.parse_args()

    asyncio.run(run_bot(
        retry_failed=args.retry_failed,
        retry_human_attention=args.retry_human_attention,
        dry_run=args.dry_run,
        num_profiles=args.num_profiles
    ))

if __name__ == "__main__":
    main()
