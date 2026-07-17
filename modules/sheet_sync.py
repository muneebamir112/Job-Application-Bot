import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import config
from modules.logger import logger

class SheetSync:
    def __init__(self):
        self.scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        self.client = self._authenticate()
        self.sheet = self._open_worksheet()
        self.headers = []
        self.col_indices = {}
        self._load_headers()

    def _authenticate(self):
        """Authenticates with Google API using service account credentials."""
        try:
            creds = Credentials.from_service_account_file(
                config.SERVICE_ACCOUNT_JSON,
                scopes=self.scopes
            )
            return gspread.authorize(creds)
        except Exception as e:
            logger.error(f"Failed to authenticate with Google Service Account: {e}")
            raise

    def _open_worksheet(self):
        """Opens the specified worksheet in the Google Sheet."""
        try:
            spreadsheet = self.client.open_by_key(config.GOOGLE_SHEET_ID)
            return spreadsheet.worksheet(config.WORKSHEET_NAME)
        except Exception as e:
            logger.error(f"Failed to open Google Sheet ID '{config.GOOGLE_SHEET_ID}' worksheet '{config.WORKSHEET_NAME}': {e}")
            raise

    def _load_headers(self):
        """Loads and maps the headers of the sheet to their column indices."""
        try:
            self.headers = self.sheet.row_values(1)
            # Normalise headers to lower case for easier matching
            self.col_indices = {header.strip().lower(): i + 1 for i, header in enumerate(self.headers)}
            
            # Verify required columns are present
            required_cols = ["company name", "job title", "job link", "status", "date added"]
            for col in required_cols:
                if col not in self.col_indices:
                    # Try fuzzy mapping or create fallback
                    logger.warning(f"Required column '{col}' not found exactly in sheet. Mapping key might fail.")
        except Exception as e:
            logger.error(f"Failed to load headers from worksheet: {e}")
            raise

    def get_pending_jobs(self, retry_failed=False, retry_human_attention=False):
        """
        Reads the sheet and returns a list of jobs that are pending processing.
        Each job is a dict with details, and includes 'row_index' (1-based sheet row).
        """
        all_rows = self.sheet.get_all_values()
        if len(all_rows) <= 1:
            logger.info("Sheet is empty or only contains headers.")
            return []

        pending_jobs = []
        status_col = self.col_indices.get("status")
        link_col = self.col_indices.get("job link")
        company_col = self.col_indices.get("company name")
        title_col = self.col_indices.get("job title")
        date_added_col = self.col_indices.get("date added")

        # Iterate starting from row 2 (index 1 in all_rows, but 2 in Sheet)
        for index, row in enumerate(all_rows[1:], start=2):
            # Pad row if columns are shorter than headers length
            while len(row) < len(self.headers):
                row.append("")
                
            status = row[status_col - 1].strip() if status_col and len(row) >= status_col else ""
            link = row[link_col - 1].strip() if link_col and len(row) >= link_col else ""
            company = row[company_col - 1].strip() if company_col and len(row) >= company_col else "Unknown"
            title = row[title_col - 1].strip() if title_col and len(row) >= title_col else "Unknown"

            # Check if job link is empty
            if not link:
                continue

            # Determine whether to process the job
            should_process = False
            if status in ("", "Pending"):
                should_process = True
            elif status == "Failed" and retry_failed:
                should_process = True
            elif status == "Human Attention" and retry_human_attention:
                should_process = True

            if should_process:
                # Add date added timestamp if empty
                if date_added_col and (len(row) < date_added_col or not row[date_added_col - 1].strip()):
                    now_str = datetime.now().strftime("%Y-%m-%d")
                    self.sheet.update_cell(index, date_added_col, now_str)
                    
                pending_jobs.append({
                    "row_index": index,
                    "company": company,
                    "title": title,
                    "link": link,
                    "status": status
                })

        return pending_jobs

    def update_status(self, row_index: int, status: str):
        """Updates the Status column for the given row index immediately."""
        status_col = self.col_indices.get("status")
        if not status_col:
            logger.error("Cannot update status: 'Status' column index is unknown.")
            return
        
        # Valid status choices only: "Submitted", "Failed", "Human Attention"
        if status not in ("Submitted", "Failed", "Human Attention"):
            logger.warning(f"Invalid status value '{status}' requested. Writing anyway.")
            
        try:
            self.sheet.update_cell(row_index, status_col, status)
            logger.info(f"Updated Sheet row {row_index} status to: '{status}'")
        except Exception as e:
            logger.error(f"Failed to update sheet row {row_index} status: {e}")
