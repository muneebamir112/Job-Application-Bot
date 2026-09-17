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
            required_cols = ["company name", "job title", "job link", "date added"]
            for col in required_cols:
                if col not in self.col_indices:
                    # Try fuzzy mapping or create fallback
                    logger.warning(f"Required column '{col}' not found exactly in sheet. Mapping key might fail.")
        except Exception as e:
            logger.error(f"Failed to load headers from worksheet: {e}")
            raise

    def get_pending_jobs(self, retry_failed=False, retry_human_attention=False, profiles_to_check=None):
        """
        Reads the sheet and returns a list of jobs that are pending processing.
        Each job is a dict with details, and includes 'row_index' (1-based sheet row)
        and 'profiles_to_apply' which maps a profile name to its column index.
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

            # If the job has already been applied to by ANY profile (or manually marked), skip it entirely
            already_applied = False
            if profiles_to_check:
                for profile_name in profiles_to_check:
                    p_col = self.col_indices.get(profile_name.lower())
                    if p_col and len(row) >= p_col:
                        p_status = row[p_col - 1].strip().lower()
                        if "submitted" in p_status or "applied" in p_status:
                            already_applied = True
                            break
            
            if already_applied:
                continue

            # Identify profiles to apply for
            profiles_to_apply = {}
            if profiles_to_check:
                for profile_name in profiles_to_check:
                    p_col = self.col_indices.get(profile_name.lower())
                    if p_col and len(row) >= p_col:
                        p_status = row[p_col - 1].strip()
                        should_apply = False
                        if p_status == "Generated":
                            should_apply = True
                        elif retry_failed and "Failed" in p_status:
                            should_apply = True
                        elif retry_human_attention and "Human Attention" in p_status:
                            should_apply = True
                        
                        if should_apply:
                            profiles_to_apply[profile_name] = p_col
            
            # Keep backwards compatibility for original processing logic if no profiles
            if not profiles_to_check:
                should_process = False
                if status in ("", "Pending"):
                    should_process = True
                elif status == "Failed" and retry_failed:
                    should_process = True
                elif status == "Human Attention" and retry_human_attention:
                    should_process = True
            else:
                # If checking specific profiles, only process if there's at least one profile to apply
                should_process = len(profiles_to_apply) > 0

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
                    "status": status,
                    "profiles_to_apply": profiles_to_apply
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

    def update_profile_status(self, row_index: int, col_index: int, status: str):
        """Updates a specific profile's status column."""
        try:
            self.sheet.update_cell(row_index, col_index, status)
            logger.info(f"Updated Sheet row {row_index}, col {col_index} profile status to: '{status}'")
        except Exception as e:
            logger.error(f"Failed to update sheet row {row_index} col {col_index} profile status: {e}")

    def log_form_answers(self, profile_name: str, ollama_answers: list):
        """
        Appends Ollama generated Q&A to 'Sheet3' in the same Google Sheet.
        Writes the header row automatically if the sheet is empty.

        ollama_answers: list of dicts with keys: system_prompt, question, answer
        """
        ANSWERS_SHEET_NAME = "Sheet3"
        HEADERS = ["Profile Name", "System Prompt", "Question", "Answer"]

        if not ollama_answers:
            return

        try:
            spreadsheet = self.client.open_by_key(config.GOOGLE_SHEET_ID)
            ws = spreadsheet.worksheet(ANSWERS_SHEET_NAME)

            # Write headers if the sheet is completely empty
            existing = ws.row_values(1)
            if not existing or not any(existing):
                ws.append_row(HEADERS)
                logger.info(f"Wrote headers to '{ANSWERS_SHEET_NAME}'.")

            rows = []
            for qa in ollama_answers:
                answer = str(qa.get("answer") or "")
                if len(answer) > 4000:
                    answer = answer[:3997] + "..."
                rows.append([
                    profile_name,
                    qa.get("system_prompt", ""),
                    qa.get("question", ""),
                    answer,
                ])

            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Logged {len(rows)} form answers to '{ANSWERS_SHEET_NAME}'.")

        except Exception as e:
            logger.error(f"Failed to log form answers to '{ANSWERS_SHEET_NAME}': {e}")
