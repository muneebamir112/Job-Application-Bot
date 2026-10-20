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
from modules.logger import logger, log_run_summary, get_job_logger, clean_filename
from modules.resume_parser import get_or_create_profile
from modules.sheet_sync import SheetSync
from modules.form_filler import fill_and_submit_form, wait_for_fields_to_settle
from modules.text_utils import normalize_company

def resolve_resume_path(company: str, profile_name: str) -> str | None:
    """
    Finds candidate resume tailored for the company or fallback.
    Checks:
    1. Folder with company name inside CVS_DIR or CVS_ALT_DIR (exact or clean):
       - {profile_name}.pdf
       - any other .pdf in that folder
    2. Flat file in CVS_DIR or CVS_ALT_DIR:
       - {profile_name} - {company}.pdf
       - {company} - {profile_name}.pdf
       - any PDF containing both profile_name and company
    3. Base candidate profile resumes:
       - profiles/{profile_name}.pdf
       - resume-bot/profiles/{profile_name}.pdf
       - resume/resume_default.pdf.pdf
    """
    search_dirs = []
    for d in (getattr(config, "CVS_DIR", None), getattr(config, "CVS_ALT_DIR", None)):
        if d and os.path.isdir(d) and d not in search_dirs:
            search_dirs.append(d)

    clean_comp = clean_filename(company).lower()

    for cv_dir in search_dirs:
        # Check folders inside cv_dir
        try:
            for entry in os.listdir(cv_dir):
                entry_path = os.path.join(cv_dir, entry)
                if os.path.isdir(entry_path):
                    if entry.lower() == company.lower() or clean_filename(entry).lower() == clean_comp:
                        prof_pdf = os.path.join(entry_path, f"{profile_name}.pdf")
                        if os.path.isfile(prof_pdf):
                            return prof_pdf
                        pdfs = [os.path.join(entry_path, f) for f in os.listdir(entry_path) if f.lower().endswith(".pdf")]
                        if pdfs:
                            match_pdf = next((p for p in pdfs if profile_name.lower() in os.path.basename(p).lower()), pdfs[0])
                            return match_pdf
        except Exception:
            pass

        # Check profile subfolder (e.g. cv_dir / profile_name)
        prof_subfolder = os.path.join(cv_dir, profile_name)
        if os.path.isdir(prof_subfolder):
            prof_candidates = [
                os.path.join(prof_subfolder, f"{profile_name} - {company}.pdf"),
                os.path.join(prof_subfolder, f"{company} - {profile_name}.pdf"),
                os.path.join(prof_subfolder, f"{company}.pdf"),
                os.path.join(prof_subfolder, f"{clean_filename(company)}.pdf"),
            ]
            for pc in prof_candidates:
                if os.path.isfile(pc):
                    return pc
            try:
                for f in os.listdir(prof_subfolder):
                    if f.lower().endswith(".pdf") and (company.lower() in f.lower() or clean_comp in clean_filename(f).lower()):
                        return os.path.join(prof_subfolder, f)
            except Exception:
                pass

        # Check flat files in cv_dir
        flat_candidates = [
            os.path.join(cv_dir, f"{profile_name} - {company}.pdf"),
            os.path.join(cv_dir, f"{company} - {profile_name}.pdf"),
            os.path.join(cv_dir, f"{clean_filename(company)}_{profile_name}.pdf"),
        ]
        for fc in flat_candidates:
            if os.path.isfile(fc):
                return fc

        try:
            for f in os.listdir(cv_dir):
                if f.lower().endswith(".pdf"):
                    f_lower = f.lower()
                    if profile_name.lower() in f_lower and (company.lower() in f_lower or clean_comp in clean_filename(f).lower()):
                        return os.path.join(cv_dir, f)
        except Exception:
            pass

    # Base profile resumes
    base_candidates = [
        os.path.join(config.PROJECT_ROOT, "profiles", f"{profile_name}.pdf"),
        os.path.join(config.PROJECT_ROOT, "..", "resume-bot", "profiles", f"{profile_name}.pdf"),
        os.path.join(config.RESUME_DIR, f"{profile_name}.pdf"),
        os.path.join(config.RESUME_DIR, "resume_default.pdf.pdf"),
    ]
    for bc in base_candidates:
        if os.path.isfile(bc):
            return bc

    return None

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
        "a:has-text('I Agree')",
        "[aria-label='Close']",
        "button[aria-label='Close']",
        "button[aria-label='close']",
        "[class*='vwo-modal'] [class*='close']",
        "[id*='vwo-widget'] [class*='close']",
        "[aria-label='Image dialog box'] [class*='close']",
        "[aria-label='Image dialog box'] button",
        ".modal-close",
        ".popup-close"
    ]
    
    for selector in selectors:
        try:
            # Quick check if any matching element is visible
            elements = await page.locator(selector).all()
            for el in elements:
                if await el.is_visible():
                    logger.info(f"Dismissing overlay using selector: {selector}")
                    await el.click(timeout=3000)
                    await asyncio.sleep(1) # wait a moment for animation to finish
        except Exception:
            pass # ignore errors if elements don't exist or become detached

async def run_bot(retry_failed: bool, retry_human_attention: bool, dry_run: bool = False, num_profiles: int = 0, specific_profile: str = ""):
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
                    if "full_name" not in p_data and "name" in p_data:
                        p_data["full_name"] = p_data["name"]
                    if "first_name" not in p_data and p_data.get("full_name"):
                        p_data["first_name"] = p_data["full_name"].split()[0]
                    if "last_name" not in p_data and p_data.get("full_name") and len(p_data["full_name"].split()) > 1:
                        p_data["last_name"] = p_data["full_name"].split()[-1]
                    if "location" not in p_data and "address" in p_data:
                        p_data["location"] = p_data["address"]
                    all_profiles[p_name] = p_data
        except Exception as e:
            logger.error(f"Failed to load profile {p_file}: {e}")

    if not all_profiles:
        logger.error("No valid profiles found in profiles directory. Please add profile JSONs.")
        sys.exit(1)
    
    logger.info(f"Loaded {len(all_profiles)} profiles: {list(all_profiles.keys())}")
    
    if specific_profile:
        # Match case-insensitively
        matched = {k: v for k, v in all_profiles.items() if specific_profile.lower() in k.lower()}
        if matched:
            all_profiles = matched
            logger.info(f"Targeting specific profile: {list(all_profiles.keys())}")
        else:
            logger.warning(f"Specific profile '{specific_profile}' not found in loaded profiles. Available: {list(all_profiles.keys())}")
    elif num_profiles > 0:
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

    # Step 3: Launch visible browser via browser-use with auto-recovery
    browser_config = BrowserConfig(
        headless=config.HEADLESS,
        disable_security=True,
        stealth=config.STEALTH_MODE
    )
    browser_holder = {"browser": None}

    async def get_fresh_context():
        """Creates a fresh browser context with auto-healing if the browser disconnected/crashed."""
        for attempt in range(3):
            try:
                if browser_holder["browser"] is None:
                    browser_holder["browser"] = Browser(config=browser_config)
                ctx = await browser_holder["browser"].new_context()
                # Test driver responsiveness by retrieving page
                test_page = await ctx.get_current_page()
                if not test_page:
                    await ctx.new_tab()
                return ctx
            except Exception as b_err:
                logger.warning(f"Browser connection error ({b_err}), resetting browser instance (attempt {attempt + 1}/3)...")
                try:
                    if browser_holder["browser"] is not None:
                        await browser_holder["browser"].close()
                except Exception:
                    pass
                browser_holder["browser"] = None
                await asyncio.sleep(1)
                if attempt == 2:
                    raise

    submitted_count = 0
    failed_count = 0
    human_attention_count = 0

    # Track companies applied to per profile during this run to prevent duplicate applications to the same company
    run_applied_companies = {
        p.lower(): set(getattr(sheet, 'applied_companies_by_profile', {}).get(p.lower(), set()))
        for p in all_profiles
    }

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

                norm_comp = normalize_company(company)
                if norm_comp and norm_comp not in ("unknown", "n/a", "na", "-"):
                    if norm_comp in run_applied_companies.get(profile_name.lower(), set()):
                        logger.info(
                            f"Skipping job {row_idx}: '{title}' at '{company}' for '{profile_name}' "
                            f"— profile has already applied to a job at this company."
                        )
                        continue

                logger.info(f"Processing job {row_idx}: {title} at {company} (Profile: {profile_name})...")
                job_logger, log_path = get_job_logger(company, title, run_timestamp)
                job_logger.info(f"Starting application: {title} at {company} for {profile_name}")
                job_logger.info(f"URL: {link}")

                resume_path_to_use = resolve_resume_path(company, profile_name)
                scrape_only_mode = False  # True when no resume exists — bot still fills & scrapes Q&A

                if resume_path_to_use:
                    job_logger.info(f"Using resume for '{company}': {resume_path_to_use}")
                    profile["resume_file_path"] = resume_path_to_use
                    try:
                        from modules.resume_parser import extract_text_from_pdf
                        profile["resume_text"] = extract_text_from_pdf(resume_path_to_use)
                    except Exception as e:
                        job_logger.error(f"Failed to extract text from resume: {e}")
                else:
                    # No resume found — proceed in scrape-only mode:
                    # open the form, fill all fields, capture every Q&A to Sheet3,
                    # but skip the actual resume upload step.
                    scrape_only_mode = True
                    job_logger.warning(
                        f"No resume found for '{company}' (Profile: {profile_name}) in {config.CVS_DIR} "
                        f"or base profile paths. Proceeding in scrape-only mode (form will be filled "
                        f"and all Q&A logged to Sheet3, but no resume will be uploaded)."
                    )
                    logger.info(f"Job {row_idx} [{profile_name}]: no resume — running in scrape-only mode.")
                    profile["resume_file_path"] = None
                    profile["resume_text"] = ""

                profile["job_title"] = title
                profile["company_name"] = company
                profile["job_description"] = sheet.get_description(row_idx)

                # Create a fresh context and page with auto-healing
                context = await get_fresh_context()
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

                    try:
                        await p_page.goto(link, timeout=45000, wait_until="domcontentloaded")
                    except Exception as goto_err:
                        job_logger.warning(f"Navigation wait warning ({goto_err}), proceeding with current page state...")

                    await wait_for_fields_to_settle(p_page.main_frame) # Bounded wait for dynamic assets

                    # Dismiss cookie banners and overlays before filling form
                    await dismiss_overlays(p_page, job_logger)

                    # Fill and Submit form (sheet is passed to log Q&A live as each question is answered)
                    status, reason = await fill_and_submit_form(page, profile, job_logger, company, link, dry_run=dry_run, sheet=sheet, profile_name=profile_name)

                    # Log outcome
                    if status == "Submitted":
                        submitted_count += 1
                        job_logger.info("Application submitted successfully!")
                        if norm_comp and norm_comp not in ("unknown", "n/a", "na", "-"):
                            run_applied_companies[profile_name.lower()].add(norm_comp)
                            logger.info(
                                f"Marked company '{company}' as applied for '{profile_name}'. "
                                f"Subsequent jobs from this company will be skipped for this profile."
                            )
                    elif status == "Human Attention":
                        human_attention_count += 1
                        job_logger.warning(f"Requires Human Attention: {reason}")
                    elif status in ("Signup Required", "Sign In Required"):
                        job_logger.warning(f"Sign In Required: {reason}")
                    elif status == "OTP Required":
                        job_logger.warning(f"OTP Required: {reason}")
                    elif status == "Dry Run":
                        job_logger.info(f"Dry run complete: {reason}")
                    else:
                        failed_count += 1
                        job_logger.error(f"Application failed: {reason}")

                    # Update live Sheet (dry runs are a local preview only - never touch the sheet)
                    if not dry_run and status != "Dry Run":
                        # Prefix depends on whether a real resume was attached:
                        # • scrape_only_mode → "Scraped | <status>" (no resume, Q&A captured to Sheet3)
                        # • normal mode      → "Generated | <status>" (resume was uploaded)
                        prefix = "Scraped" if scrape_only_mode else "Generated"
                        sheet.update_profile_status(row_idx, col_idx, f"{prefix} | {status}")

                except Exception as e:
                    failed_count += 1
                    import traceback
                    tb = traceback.format_exc()
                    job_logger.error(f"Unexpected exception during processing: {e}\n{tb}")
                    logger.error(f"Job {row_idx} failed for {profile_name} with unexpected exception: {e}")
                    if not dry_run:
                        sheet.update_profile_status(row_idx, col_idx, "Generated | Failed")

                finally:
                    if context:
                        try:
                            await context.close()
                        except Exception:
                            browser_holder["browser"] = None
                    logger.info(f"Finished job {row_idx} processing for {profile_name}. Log saved to {log_path}")

    finally:
        # Close browser session cleanly
        if browser_holder["browser"] is not None:
            try:
                await browser_holder["browser"].close()
            except Exception:
                pass

    # Log overall run summary
    log_run_summary(submitted_count, failed_count, human_attention_count)

def main():
    parser = argparse.ArgumentParser(description="Job Application Automation Bot")
    parser.add_argument("--retry-failed", action="store_true", help="Retry applications with 'Failed' status")
    parser.add_argument("--retry-human-attention", action="store_true", help="Retry applications with 'Human Attention' status")
    parser.add_argument("--dry-run", action="store_true", help="Fill out forms but stop before the final submit click; never updates the sheet")
    parser.add_argument("--num-profiles", type=int, default=0, help="Number of profiles to apply for. If 0, apply for all profiles.")
    parser.add_argument("--fill-required-only", action="store_true", help="Only fill compulsory fields and ignore optional fields.")
    parser.add_argument("--profile", type=str, default="", help="Specific profile to use (e.g. 'Brian Moore'). Overrides other profile selections.")
    parser.add_argument("--skip-resume", action="store_true", help="Skip uploading the resume file and bypass resume PDF checks.")
    args = parser.parse_args()
    
    import config
    config.FILL_REQUIRED_ONLY = args.fill_required_only
    if args.skip_resume:
        config.SKIP_RESUME_UPLOAD = True

    asyncio.run(run_bot(
        retry_failed=args.retry_failed,
        retry_human_attention=args.retry_human_attention,
        dry_run=args.dry_run,
        num_profiles=args.num_profiles,
        specific_profile=args.profile
    ))

if __name__ == "__main__":
    main()
