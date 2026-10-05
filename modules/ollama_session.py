import json
import logging
import asyncio
import os
import aiohttp
import config
from datetime import datetime
from modules.logger import clean_filename

logger = logging.getLogger(__name__)

class OllamaJobSession:
    def __init__(self, profile: dict, resume_text: str, company: str, job_title: str, job_description: str = ""):
        self.session_id = f"{clean_filename(company)}_{clean_filename(job_title)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.company = company
        self.job_title = job_title

        # Strip heavy/irrelevant fields to avoid context overflow
        _EXCLUDE_KEYS = {"resume_file_path", "resume_text", "cover_letter_text", "cover_letter_file_path"}
        slim_profile = {k: v for k, v in profile.items() if k not in _EXCLUDE_KEYS}
        profile_json = json.dumps(slim_profile, indent=2, ensure_ascii=False)[:3000]

        # Cap resume to avoid blowing past num_ctx
        resume_text = (resume_text or "")[:2000]
        job_description = (job_description or "")[:500]

        system_prompt = f"""You are operating inside a single job-application session.
The candidate profile, tailored resume, job description, and application rules provided below are the authoritative context for all questions in this application.
Maintain consistency with previous answers. However, never treat a previous answer as proof of a candidate fact if that answer conflicts with the candidate profile or resume.
Never invent experience, skills, employment history, education, certifications, years of experience, authorization, demographic information, or other facts.
If a question asks for information that is not supported by the candidate profile or resume, follow the application's predefined fallback rules.
Answer only the current application question. Do not include markdown, explanations, or introductory text. Output the exact answer only.

CANDIDATE PROFILE:
{profile_json}

TAILORED RESUME:
{resume_text}

JOB DETAILS:
Company: {company}
Position: {job_title}
Job Description: {job_description}

APPLICATION RULES:
CRITICAL INSTRUCTION FOR SALARY: If a question asks whether a target salary range meets your requirements, expectations, or if you accept it, you MUST ALWAYS select 'Yes'. Do not select 'No' for salary acceptance questions.
CRITICAL INSTRUCTION FOR SKILLS/TOOLS: If a question asks about your years of experience or proficiency with a specific tool, technology, or skill (e.g., Unreal Engine, C++, Golang) and that specific tool is NOT explicitly mentioned in your Profile's skills list (ignore the Resume summary for this check), you MUST strictly select the option indicating '0 years', 'None', or 'No experience'. DO NOT extrapolate. DO NOT assume you have experience with it just because you have 8+ years of general software engineering experience. If the keyword is missing from your skills list, your experience is strictly 0.
CRITICAL INSTRUCTION FOR AFFILIATIONS/HISTORY: If a question asks if you have previously worked for the company, have family members at the company, are a member of a specific tribe/nation (e.g., Seneca Nation), or have worked for the Federal/State Government, you MUST strictly select 'No'.
CRITICAL INSTRUCTION FOR OPEN-ENDED: When answering open-ended questions, always answer in the first person ('I', 'my', 'me'). NEVER refer to 'the candidate', 'the profile', or yourself as an AI. Keep answers concise, professional, and specific (2-3 sentences). Only reply with EXACTLY 'N/A' if the question asks for a specific factual URL or account link (like a Twitter/GitHub URL) that is completely missing from the profile. Never reply N/A to subjective, experience-based, preference, or opinion questions.
"""
        self.messages = [{"role": "system", "content": system_prompt.strip()}]
        self.host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = config.OLLAMA_MODEL


    async def _chat_with_ollama(self, new_user_content: str, temperature: float = 0.1, num_predict: int = None) -> str:
        # Keep system prompt + last 6 messages (3 turns) to prevent context overflow
        if len(self.messages) > 7:
            self.messages = [self.messages[0]] + self.messages[-6:]
            
        self.messages.append({"role": "user", "content": new_user_content})
        
        options = {
            "temperature": temperature,
            "num_ctx": 8192
        }
        if num_predict:
            options["num_predict"] = num_predict

        payload = {
            "model": self.model,
            "messages": self.messages,
            "stream": False,
            "options": options
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.host}/api/chat", json=payload, timeout=aiohttp.ClientTimeout(total=300)) as response:
                    if response.status == 200:
                        data = await response.json()
                        answer = data.get("message", {}).get("content", "").strip()
                        self.messages.append({"role": "assistant", "content": answer})
                        return answer
                    else:
                        text = await response.text()
                        logger.error(f"Ollama chat API returned {response.status}: {text}")
                        # On failure, pop the user message to keep state clean
                        self.messages.pop()
                        return ""
        except Exception as e:
            logger.error(f"Error communicating with Ollama chat API: {e}")
            self.messages.pop()
            return ""

    async def ask_open_ended(self, question: str, job_logger) -> str:
        prompt = f"Application question:\n\n{question}\n\nAnswer according to the candidate context. Do not include markdown, prefixes, or explanations."
        job_logger.info(f"--- OLLAMA SESSION PROMPT FOR '{question}' ---\nUser: {prompt}\n----------------------------------")
        
        answer = await self._chat_with_ollama(prompt, temperature=0.7, num_predict=200)
        
        # Clean answer
        cleaned = answer.replace('"', '').strip()
        if "N/A" in cleaned.upper():
            cleaned = "N/A"
        
        job_logger.info(f"--- OLLAMA SESSION RESPONSE FOR '{question}' ---\nRaw: {answer}\nCleaned: {cleaned}\n------------------------------------")
        
        if not hasattr(job_logger, "ollama_answers"):
            job_logger.ollama_answers = []
        job_logger.ollama_answers.append({"question_with_prompt": prompt, "question": question, "answer": cleaned, "system_prompt": self.messages[0]['content'][:1000] + "..."})
        return cleaned

    async def ask_choice(self, question: str, options: list, job_logger) -> str:
        options_str = "\n".join([f"- {opt}" for opt in options])
        prompt = f"Application question:\n\n{question}\n\nAvailable options:\n{options_str}\n\nReturn ONLY the exact text of one of the available options. Do not explain."
        job_logger.info(f"--- OLLAMA SESSION PROMPT (CHOICE) FOR '{question}' ---\nUser: {prompt}\n----------------------------------")
        
        answer = await self._chat_with_ollama(prompt, temperature=0.0, num_predict=50)
        cleaned = answer.replace('"', '').strip()
        
        # If Ollama returned empty (context overflow or model failure), use keyword fallback
        if not cleaned and options:
            question_lower = question.lower()
            if any(kw in question_lower for kw in ("salary", "meet your", "requirements", "accept")):
                cleaned = next((o for o in options if "yes" in o.lower()), options[0])
                job_logger.warning(f"Ollama returned empty for '{question}' - salary rule applied: '{cleaned}'")
            elif any(kw in question_lower for kw in ("previously worked", "family member", "tribe", "nation", "government")):
                cleaned = next((o for o in options if "no" in o.lower()), options[-1])
                job_logger.warning(f"Ollama returned empty for '{question}' - affiliation rule applied: '{cleaned}'")
            else:
                cleaned = options[0]
                job_logger.warning(f"Ollama returned empty for '{question}' - defaulting to first option: '{cleaned}'")
        
        job_logger.info(f"--- OLLAMA SESSION RESPONSE (CHOICE) FOR '{question}' ---\nRaw: {answer}\nCleaned: {cleaned}\n------------------------------------")
        
        if not hasattr(job_logger, "ollama_answers"):
            job_logger.ollama_answers = []
        job_logger.ollama_answers.append({"question_with_prompt": prompt, "question": question, "answer": cleaned, "system_prompt": self.messages[0]['content'][:1000] + "..."})
        return cleaned
    
    def save_transcript(self, logs_dir: str):
        try:
            filepath = os.path.join(logs_dir, f"{self.session_id}_session.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({"session_id": self.session_id, "company": self.company, "job_title": self.job_title, "messages": self.messages}, f, indent=2)
            logger.info(f"Saved Ollama session transcript to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save Ollama session transcript: {e}")
