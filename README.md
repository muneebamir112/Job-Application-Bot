# Job Application Automation Bot

An automated job application filler that parses your resume into a structured profile, reads target jobs from a live Google Sheet, fills out fields on the job sites using `browser-use` and local Ollama, and automatically updates the application status on the sheet.

## Features
- **Resume Parsing**: Automatically converts a `.pdf` or `.docx` resume inside the `resume/` directory into a structured JSON profile (`profile.json`) using Ollama.
- **Google Sheets Sync**: Fetches target rows from Google Sheets, updates them in real time, and adds timestamps.
- **CAPTCHA & Auth Wall Protection**: Detects captchas, registration walls, and login boxes, safely flagging them as `Human Attention` without proceeding.
- **Stealth Browsing**: Routes the browser through `patchright` + real Chrome (`STEALTH_MODE` in `.env`) instead of plain Playwright/Chromium, to avoid the automation fingerprints that trigger CAPTCHA/anti-bot challenges on some job sites in the first place.
- **AI-Powered Form Filling**: Dynamically answers open-ended and multiple-choice questions matching your profile context.

---

## Setup Instructions

### 1. Prerequisites & Installation
Ensure you have Python 3.11+ installed. Run the following commands to install dependencies:
```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Ollama Integration
1. Download and install Ollama from [ollama.com](https://ollama.com).
2. Start the Ollama background service (`ollama serve`).
3. Download the default reasoning model:
   ```bash
   ollama pull llama3.1
   ```

### 3. Google Sheet & Service Account Configuration
To enable direct read/write access to your live Google Sheet:
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project or select an existing one.
3. Search for and enable the **Google Sheets API** and **Google Drive API**.
4. Navigate to **IAM & Admin** -> **Service Accounts** and click **Create Service Account**.
5. Give the account a name, click done. Select the newly created account and go to the **Keys** tab.
6. Click **Add Key** -> **Create New Key**, choose **JSON**, and download it.
7. Rename the downloaded file to `service_account.json` and place it in the root directory of this project.
8. Copy the `client_email` address from the service account JSON.
9. Open your target Google Sheet in your web browser and click **Share**. Add the copied client email address and give it **Editor** permissions.

Ensure your Google Sheet contains these exact headers on the first row:
`No`, `Company Name`, `Job Title`, `Location`, `Job Age`, `Job Link`, `Status`, `Date Added`.

### 4. Environment Configuration
Create a `.env` file in the project root by copying the template:
```bash
cp .env.example .env
```
Fill out the variables inside `.env`:
- `GOOGLE_SHEET_ID`: Found in the sheet's URL `https://docs.google.com/spreadsheets/d/<GOOGLE_SHEET_ID>/edit`.
- `WORKSHEET_NAME`: The tab name, usually `Sheet1`.

---

## How to Run

1. Place your resume (either `resume.pdf` or `resume.docx`) in the `resume/` directory.
2. Run the orchestrator:
   ```bash
   python main.py
   ```
3. Options:
   - To retry applications marked as `Failed`, run:
     ```bash
     python main.py --retry-failed
     ```
   - To retry applications marked as `Human Attention`, run:
     ```bash
     python main.py --retry-human-attention
     ```
   - To preview a form fill without actually submitting or touching the sheet (useful when trying a new site for the first time), run:
     ```bash
     python main.py --dry-run
     ```
