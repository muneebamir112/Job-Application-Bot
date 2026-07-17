import os
import csv
import logging
from datetime import datetime
import config

# Main application logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("job_bot")

def clean_filename(name: str) -> str:
    """Removes characters that are invalid in file names."""
    return "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).rstrip().replace(" ", "_")

def get_job_logger(company: str, job_title: str):
    """
    Creates and returns a file logger specifically for a single job application.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_company = clean_filename(company) or "unknown_company"
    safe_title = clean_filename(job_title) or "unknown_title"
    log_filename = f"{safe_company}_{safe_title}_{timestamp}.log"
    log_path = os.path.join(config.LOGS_DIR, log_filename)

    job_logger = logging.getLogger(f"job_{timestamp}")
    job_logger.setLevel(logging.INFO)
    
    # Avoid duplicate handlers if logger already exists
    if job_logger.hasHandlers():
        job_logger.handlers.clear()

    # File handler for this specific job
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    job_logger.addHandler(file_handler)
    
    return job_logger, log_path

def log_run_summary(submitted: int, failed: int, human_attention: int):
    """
    Appends the run results summary to logs/run_summary.csv and prints to console.
    """
    summary_path = os.path.join(config.LOGS_DIR, "run_summary.csv")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Write header if file doesn't exist
    file_exists = os.path.exists(summary_path)
    
    try:
        with open(summary_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Submitted", "Failed", "Human Attention"])
            writer.writerow([timestamp, submitted, failed, human_attention])
    except Exception as e:
        logger.error(f"Failed to write to run_summary.csv: {e}")

    logger.info("================ RUN SUMMARY ================")
    logger.info(f"Submitted:       {submitted}")
    logger.info(f"Failed:          {failed}")
    logger.info(f"Human Attention: {human_attention}")
    logger.info("=============================================")
