import asyncio
import argparse
import sys
import os
from browser_use import Browser, BrowserConfig
import config
from modules.logger import logger, log_run_summary, get_job_logger
from modules.resume_parser import get_or_create_profile
from modules.sheet_sync import SheetSync
from modules.form_filler import fill_and_submit_form, wait_for_fields_to_settle

async def run_bot(retry_failed: bool, retry_human_attention: bool, dry_run: bool = False):
    logger.info("Initializing Job Application Automation Bot...")

    # Step 1: Check and load profile
    try:
        profile = get_or_create_profile()
        if not profile.get("resume_file_path"):
            logger.error("No resume found in resume/ directory. Please place resume.pdf or resume.docx there and re-run.")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to initialize profile: {e}")
        sys.exit(1)

    # Step 2: Initialize Google Sheet
    try:
        sheet = SheetSync()
        pending_jobs = sheet.get_pending_jobs(
            retry_failed=retry_failed,
            retry_human_attention=retry_human_attention
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
        context = await browser.new_context()
        
        for job in pending_jobs:
            row_idx = job["row_index"]
            company = job["company"]
            title = job["title"]
            link = job["link"]

            logger.info(f"Processing job {row_idx}: {title} at {company}...")
            job_logger, log_path = get_job_logger(company, title)
            job_logger.info(f"Starting application: {title} at {company}")
            job_logger.info(f"URL: {link}")

            try:
                # Get or create page
                page = await context.get_current_page()
                if not page:
                    page = await context.new_page()
                
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

                # Fill and Submit form
                status, reason = await fill_and_submit_form(page, profile, job_logger, dry_run=dry_run)

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
                if not dry_run:
                    sheet.update_status(row_idx, status)

            except Exception as e:
                failed_count += 1
                import traceback
                tb = traceback.format_exc()
                job_logger.error(f"Unexpected exception during processing: {e}\n{tb}")
                logger.error(f"Job {row_idx} failed with unexpected exception: {e}")
                if not dry_run:
                    sheet.update_status(row_idx, "Failed")

            logger.info(f"Finished job {row_idx} processing. Log saved to {log_path}")

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
    args = parser.parse_args()

    asyncio.run(run_bot(
        retry_failed=args.retry_failed,
        retry_human_attention=args.retry_human_attention,
        dry_run=args.dry_run
    ))

if __name__ == "__main__":
    main()
