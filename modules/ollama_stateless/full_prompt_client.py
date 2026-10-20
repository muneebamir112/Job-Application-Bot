import json
import logging
import os
import re
import aiohttp
import config
from modules.text_utils import strip_markdown_formatting
from modules.sheet_sync import SheetSync

logger = logging.getLogger(__name__)

class FullPromptOllamaClient:
    """
    Stateless Ollama client that asks EVERY question with a complete,
    self-contained system prompt and profile context instead of maintaining
    an accumulating multi-turn conversation history.
    """

    def __init__(self, profile: dict, company: str, job_title: str, job_description: str = ""):
        self.company = company
        self.job_title = job_title
        self.host = os.getenv("OLLAMA_HOST", getattr(config, "OLLAMA_HOST", "http://localhost:11434"))
        self.model = getattr(config, "OLLAMA_MODEL", "qwen3.5:9b")

        # Strip binary / large irrelevant paths
        _EXCLUDE_KEYS = {"resume_file_path", "cover_letter_file_path", "cover_letter_text"}
        self.slim_profile = {k: v for k, v in profile.items() if k not in _EXCLUDE_KEYS}
        self.profile_json = json.dumps(self.slim_profile, indent=2, ensure_ascii=False)[:3500]
        self.job_description = (job_description or "")[:800]

        # Loggable prompt template stored in Sheet3 col-B
        self.sheet_prompt = (
            f"You are the candidate applying for this job. "
            f"The Candidate Profile and Resume provided are YOUR personal background, YOUR experience, and YOUR identity. "
            f"You MUST answer the question in the first person ('I', 'my', 'me'). "
            f"NEVER refer to 'the candidate', 'the profile', 'the resume', or yourself as an AI. "
            f"If a question asks for something not explicitly stated in your background, use your intelligence and professional judgment to deduce a reasonable, realistic answer as if you were this person. "
            f"IMPORTANT: Only reply with EXACTLY 'N/A' if the question asks for a specific factual URL or account link (like a Twitter/GitHub URL) that is completely missing from the profile. "
            f"For ALL other questions — including subjective, experience-based, preference, or opinion questions — you MUST write a proper first-person answer. "
            f"NEVER reply N/A to those. Keep your answer concise, professional, and specific (2-4 sentences). "
            f"This answer is typed directly into a plain-text form field, so write in plain prose only: no markdown, no **bold**, no headers, no bullet points, or asterisks. "
            f"You are applying for the '{self.job_title}' position at '{self.company}'. "
            f"If the question or answer references the role or company, use those exact names. "
            f"Never output placeholder text like [Company]."
        )

    def _build_full_prompt(self, question: str, options: list[str] = None) -> tuple[str, str]:
        """
        Builds the complete system prompt and user prompt containing the full
        candidate background and explicit instructions for this specific question.
        """
        system_prompt = f"""You are the candidate applying for a job.
You are Brian Moore applying for the '{self.job_title}' position at '{self.company}'.
The Candidate Profile and details provided below are YOUR personal background, YOUR experience, and YOUR identity.
You MUST answer in the first person ('I', 'my', 'me'). NEVER refer to yourself as an AI or third party.
Never invent employment history, certifications, or employers not present in the context.

CANDIDATE BACKGROUND:
{self.profile_json}

TARGET JOB:
Company: {self.company}
Position: {self.job_title}
Job Description: {self.job_description}

APPLICATION RULES:
1. SALARY: If a question asks whether a target salary range meets your requirements, expectations, or if you accept it, you MUST ALWAYS select or answer 'Yes'.
2. SKILLS/EXPERIENCE: If asked for years of experience or proficiency with a specific tool/technology not in your profile, strictly answer '0' or 'None' or 'No experience'.
3. AFFILIATIONS/HISTORY: If asked if you have previously worked for this company, have family members at this company, or worked for the Government, strictly select 'No'.
4. STYLE: Answer directly in plain text. No markdown, no asterisks, no bullet points, no commentary.
"""
        if options:
            opts_formatted = "\n".join(f"- {opt}" for opt in options)
            user_prompt = f"""Question: {question}

Available Options:
{opts_formatted}

Select ONLY ONE option verbatim from the available options list that best answers the question based on your profile.
Output the exact option text only. Do not add quotes, explanations, or introductory text."""
        else:
            user_prompt = f"""Question: {question}

Instructions:
Answer this question in the first person ('I', 'my', 'me') as Brian Moore.
Keep your answer concise, professional, and realistic (2-3 sentences).
Only reply with EXACTLY 'N/A' if the question asks for a specific factual URL or account link (like a Twitter/GitHub URL) that is completely missing from your profile.
Provide the direct text answer only:"""

        return system_prompt.strip(), user_prompt.strip()

    async def _query_ollama(self, system_prompt: str, user_prompt: str, temperature: float = 0.2, num_predict: int = 400) -> str:
        url = f"{self.host.rstrip('/')}/api/generate"
        payload = {
            "model": self.model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": temperature,
                "num_predict": num_predict
            }
        }
        timeout = aiohttp.ClientTimeout(total=max(120, getattr(config, "OLLAMA_LONG_TIMEOUT", 120)))
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data.get("response", "").strip()
                        else:
                            txt = await resp.text()
                            logger.error(f"Ollama returned {resp.status}: {txt}")
            except Exception as e:
                logger.warning(f"Ollama attempt {attempt + 1}/3 failed: {e}")
                import asyncio
                await asyncio.sleep(2)
        return ""

    async def ask_open_ended(self, question: str, job_logger) -> str:
        """Asks Ollama an open-ended question using the full prompt."""
        system_prompt, user_prompt = self._build_full_prompt(question)
        job_logger.info(f"--- STATELSS OLLAMA FULL PROMPT FOR '{question}' ---\n{user_prompt}\n----------------------------------")
        raw = await self._query_ollama(system_prompt, user_prompt, temperature=0.3, num_predict=500)
        cleaned = strip_markdown_formatting(raw).strip()
        job_logger.info(f"--- STATELSS OLLAMA RESPONSE FOR '{question}' ---\n{cleaned}\n------------------------------------")

        if not hasattr(job_logger, "ollama_answers"):
            job_logger.ollama_answers = []
        job_logger.ollama_answers.append({
            "question_with_prompt": self.sheet_prompt,
            "question": question,
            "answer": cleaned
        })
        return cleaned

    async def ask_choice(self, question: str, options: list[str], job_logger) -> str:
        """Asks Ollama to select from multiple options using the full prompt."""
        system_prompt, user_prompt = self._build_full_prompt(question, options=options)
        job_logger.info(f"--- STATELESS OLLAMA CHOICE PROMPT FOR '{question}' ---\n{user_prompt}\n----------------------------------")
        raw = await self._query_ollama(system_prompt, user_prompt, temperature=0.0, num_predict=150)
        cleaned = strip_markdown_formatting(raw).strip()
        cleaned = re.sub(r"^['\"\[]+|['\"\]]+$", "", cleaned).strip()

        # Fallback rules if empty
        if not cleaned:
            q_norm = question.lower()
            if any(k in q_norm for k in ("salary", "compensation", "rate", "accept")):
                cleaned = next((o for o in options if "yes" in o.lower()), options[0] if options else "")
            elif any(k in q_norm for k in ("relative", "family", "previously worked", "government", "felony")):
                cleaned = next((o for o in options if "no" in o.lower()), options[0] if options else "")
            else:
                cleaned = options[0] if options else ""

        job_logger.info(f"--- STATELESS OLLAMA CHOICE RESPONSE FOR '{question}' ---\n{cleaned}\n------------------------------------")

        if not hasattr(job_logger, "ollama_answers"):
            job_logger.ollama_answers = []
        job_logger.ollama_answers.append({
            "question_with_prompt": self.sheet_prompt,
            "question": question,
            "answer": cleaned
        })
        return cleaned
