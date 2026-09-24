# Job-Bot Workflow & Architecture Documentation

This document provides a comprehensive overview of the Job-Bot application, explaining its complete workflow step-by-step, the purpose of each module, and specific technical details on how the automation operates.

## 1. High-Level Workflow Overview

The Job-Bot is an automated, AI-assisted tool designed to read job applications from a Google Sheet, navigate to the application URLs using headless/headed browser automation (Playwright), fill out complex dynamic forms using user profile data, solve or pause for captchas, and submit the application. Finally, it updates the Google Sheet with the application's final status.

### The Lifecycle of a Run
1. **Initialization**: The bot starts via `main.py`. It initializes the logger, loads configuration settings and environment variables (via `config.py`), and loads the user's profile data (`profiles/Brian Moore.json` or `profile.json`).
2. **Sheet Synchronization**: The bot connects to the configured Google Sheet via `modules/sheet_sync.py`. It pulls rows of jobs, specifically looking for ones with the status "Pending".
3. **Browser Setup**: A Playwright browser instance is launched. The bot applies patches (`browser_use_patch.py`) to bypass bot-detection mechanisms (like Cloudflare).
4. **Job Iteration**: For each pending job URL:
   - The bot navigates to the URL.
   - It runs `modules/captcha_detector.py` to ensure the page hasn't hit a CAPTCHA block.
   - It identifies the application form and passes the page context to `modules/form_filler.py`.
5. **Form Processing (The Core)**:
   - **Discovery**: The bot scans the DOM (including Shadow DOMs for complex sites like SmartRecruiters) to find all inputs, selects, and comboboxes.
   - **Classification**: Each field is categorized based on its label.
   - **Data Mapping**: The bot attempts to map the field to deterministic profile data using hardcoded rules and keywords.
   - **AI Fallback**: If a field cannot be deterministically matched, the bot uses `modules/ollama_client.py` to ask a local LLM for the best matching response from the options or profile.
   - **Filling & Interacting**: The bot executes Playwright actions to fill the data, select dropdowns, and bypass custom UI widgets (like `select2` for Greenhouse).
   - **Validation Loop**: Before moving to the next page or submitting, it triggers validation checks, scans for error messages (`aria-invalid`), and performs "Targeted Recovery" to fix any fields that the site rejected.
6. **Submission & Reporting**: The bot submits the form, logs the success or failure, and updates the Google Sheet row to "Applied" or "Failed" via `sheet_sync.py`.

---

## 2. File & Directory Breakdown

### Root Directory
* **`main.py`**: The entry point of the application. It orchestrates the entire workflow. It handles the main event loop, sets up the Playwright browser contexts, loops through the Google Sheets data, and invokes the form filler for each page.
* **`config.py`**: Centralized configuration management. Loads environment variables (from `.env`), sets timeout thresholds, debug flags, and stores global constants.
* **`profile.json` & `profiles/`**: Contains the structured personal data (JSON) of the applicant (e.g., education, work history, demographics, GitHub links). This acts as the "source of truth" for the form filler.
* **`.env`**: Stores sensitive API keys, Google Sheet IDs, and local LLM configurations.

### `modules/` Directory (Core Logic)
* **`modules/form_filler.py`**: The most critical and largest file in the bot. It contains the primary logic for identifying, classifying, and filling form elements.
  * *Field Discovery*: Uses robust `query_selector_all` combined with Javascript execution to find standard inputs and shadow DOM elements.
  * *Text Fields*: Maps input labels (e.g., "First Name") to the JSON profile.
  * *Select & Comboboxes*: Handles standard `<select>` tags and complex custom dropdowns (like React-Select or Select2 used heavily on Greenhouse and Lever). Includes intricate logic to open dropdowns, find visible options, and force-click them if necessary.
  * *Hardcoded Rules*: Uses keyword matching (`ALWAYS_NO_LABEL_KEYWORDS`, `ALWAYS_YES_LABEL_KEYWORDS`) to safely navigate sensitive EEO, demographic, and sponsorship questions without relying on AI hallucination.
  * *Validation Recovery*: After attempting to fill a form, it actively looks for fields that threw validation errors (like `aria-invalid='true'`) and attempts to re-fill them.
* **`modules/ollama_client.py`**: A wrapper for the local Ollama LLM API. When `form_filler.py` encounters a custom question it doesn't have a hardcoded rule for, it packages the question and the user's profile and asks Ollama to infer the best answer.
* **`modules/sheet_sync.py`**: Handles all Google Sheets API interactions using the `service_account.json` credentials. It fetches pending jobs and writes back statuses ("Applied", "Error: Captcha", etc.) along with timestamps.
* **`modules/captcha_detector.py`**: Runs in the background during navigation. It looks for known CAPTCHA elements (Cloudflare Turnstile, reCAPTCHA, hCaptcha). If detected, it can either pause the execution for manual user intervention or attempt specific bypass strategies.
* **`modules/logger.py`**: Custom logging setup. It creates detailed timestamped log files in the `logs/` directory for each job run, making debugging (like validation loop failures) traceable.
* **`modules/resume_parser.py`**: Helper functions that can extract or parse text from PDF resumes in the `resume/` folder, usually for uploading or extracting raw text to paste into text boxes.
* **`modules/text_utils.py`**: Utility functions for normalizing text (removing punctuation, lowercasing) to make fuzzy matching between form labels and profile keys more reliable.
* **`modules/browser_use_patch.py`**: Contains specific overrides and patches for the Playwright browser instance to spoof user agents, modify navigator properties, and avoid basic bot-detection scripts.

---

## 3. Deep Dive: Key Mechanisms in `form_filler.py`

Because `form_filler.py` is the brain of the operation, here is a detailed breakdown of its execution flow:
1. **`process_form_fields()`**: The main loop. It gathers all interactable elements (`input`, `select`, `[role='combobox']`, etc.) and determines their type.
2. **Classification**: `classify_field()` evaluates the label text and decides if it's a `PROFILE_FIELD` (Name, Email), a `CHOICE_FIELD` (Yes/No, Visa questions), or something else.
3. **Handling Comboboxes (`handle_combobox_field`)**: For custom UI dropdowns that hide the native `<select>` tag, the bot must:
   - Click the visible `<span>` container to open the dropdown menu.
   - Wait for the dropdown options to attach to the DOM.
   - Run `find_visible_listbox_options()` to scan the DOM for the newly rendered options.
   - Execute `_attempt_click_from_options()` to match the profile data against the visible options and click the corresponding DOM element. If the click fails due to obstruction, it falls back to a `force=True` click or triggers native jQuery `change` events to force the site's state to update.
4. **Targeted Recovery**: If a site's backend validation rejects an input (e.g., the bot typed the data but React didn't register the state change), the site will mark the field with `aria-invalid="true"` or `.error` classes. The bot detects this, scrolls to the broken field, and performs a more aggressive, character-by-character typing approach or native event dispatching to satisfy the site's strict event listeners.
