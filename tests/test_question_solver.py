"""
Hierarchical RAG + Ollama Question Solver Test Script
Location: tests/test_question_solver.py

Architecture:
- Layer 1: Core profile (name, contact, work auth, visa status, years exp, base skills)
- Layer 2: Detailed candidate knowledge (work experience, projects, responsibilities)
- Layer 3: Job context (company name, job title, role details)

Supports:
1. Single-choice (dropdown/radio)
2. Multi-choice (checkboxes)
3. Open-ended / descriptive text
4. Numeric / years of experience
"""

import os
import sys
import json
import time
import re
import argparse
import requests

# Ensure parent directory is in sys.path so we can import config & modules
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config


class CandidateContextManager:
    """Manages the 3 layers of candidate & job context."""

    def __init__(self, profile_path: str = None, extra_info_path: str = None):
        self.profile_path = profile_path or config.PROFILE_JSON_PATH
        self.extra_info_path = extra_info_path or config.EXTRA_INFO_JSON_PATH
        self.profile = self._load_json(self.profile_path)
        self.extra_info = self._load_json(self.extra_info_path)

        # Merge extra_info into profile for fields missing in profile.json
        if self.extra_info:
            for k, v in self.extra_info.items():
                if v and not self.profile.get(k):
                    self.profile[k] = v

    def _load_json(self, path: str) -> dict:
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[!] Warning: Failed to load {path}: {e}")
        return {}

    def get_layer_1_core(self) -> dict:
        """Layer 1: Small facts frequently needed."""
        p = self.profile
        return {
            "name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "location": p.get("location", "Haltom City, TX, United States"),
            "email": p.get("email", ""),
            "phone": p.get("phone", ""),
            "work_authorization": p.get("work_authorization") or "Legally authorized to work in the United States for any employer",
            "visa_sponsorship_needed": p.get("visa_sponsorship_needed") or "No",
            "years_of_experience": p.get("years_experience") or "8+ years",
            "current_title": p.get("current_title") or "Software Development Engineer V",
            "salary_expectation": p.get("salary_expectation") or "130k",
            "skills": p.get("skills", []),
            "education": p.get("education", []),
            "demographics": {
                "gender": p.get("gender", "Male"),
                "race": p.get("race", "I don't wish to answer"),
                "veteran": p.get("veteran", "No, not a veteran"),
                "disability": p.get("disability", "No")
            }
        }

    def get_layer_2_detailed(self) -> dict:
        """Layer 2: Detailed candidate knowledge (work history, achievements)."""
        p = self.profile
        work = []
        for w in p.get("work_experience", []):
            work.append({
                "company": w.get("company"),
                "title": w.get("title"),
                "period": f"{w.get('start', '')} - {w.get('end', '')}",
                "summary": w.get("summary", "")
            })
        return {
            "work_history": work,
            "resume_summary": (p.get("resume_text", "")[:1200] if p.get("resume_text") else "")
        }

    def get_layer_3_job(self, company: str = "Target Employer", job_title: str = "Senior Software Engineer", job_description: str = "") -> dict:
        """Layer 3: Job context."""
        return {
            "company": company,
            "job_title": job_title,
            "job_description_snippet": job_description[:500] if job_description else "Software engineering role"
        }

    def build_context_for_question(self, question: str, company: str = "Target Employer", job_title: str = "Senior Software Engineer") -> dict:
        """
        Dynamically assembles the minimal necessary context layers for the given question.
        Prevents prompt bloat and improves LLM precision.
        """
        q_lower = question.lower()
        context = {"layer_1_core": self.get_layer_1_core()}

        # Check if question needs detailed experience / open-ended project answers
        needs_layer_2 = any(kw in q_lower for kw in [
            "describe", "explain", "project", "system", "tell us", "built", "experience with",
            "architecture", "challenge", "split between", "why", "most proud", "scalable"
        ])
        if needs_layer_2:
            context["layer_2_detailed"] = self.get_layer_2_detailed()

        # Always include minimal job context
        context["layer_3_job"] = self.get_layer_3_job(company, job_title)
        return context


class OllamaQuestionSolver:
    """Answers job application questions using Ollama and Hierarchical Context."""

    def __init__(self, model: str = None, host: str = None):
        self.host = host or config.OLLAMA_HOST
        self.model = model or self._detect_or_fallback_model(config.OLLAMA_MODEL)
        self.context_manager = CandidateContextManager()

    def _detect_or_fallback_model(self, configured_model: str) -> str:
        """Checks if the configured model exists in Ollama; otherwise picks an available one."""
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=3)
            if resp.status_code == 200:
                models = [m.get("name") for m in resp.json().get("models", [])]
                if configured_model in models:
                    return configured_model
                # Try finding matching prefix (e.g. qwen3.5:9b or qwen3)
                for m in models:
                    if configured_model.split(":")[0] in m:
                        return m
                if models:
                    print(f"[*] Configured model '{configured_model}' not found in Ollama. Using available model '{models[0]}'")
                    return models[0]
        except Exception:
            pass
        return configured_model

    def _call_ollama(self, prompt: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 500) -> str:
        url = f"{self.host}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": 4096
            }
        }
        if system_prompt:
            payload["system"] = system_prompt

        timeout = int(os.getenv("OLLAMA_TIMEOUT", "120"))
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code == 200:
            raw = resp.json().get("response", "").strip()
            # If model produced a <think> tag, try getting text outside first
            outside_think = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            if outside_think:
                return outside_think
            # If think tag was unclosed or occupied whole response, strip the <think> opening tag
            cleaned = re.sub(r"</?think>", "", raw).strip()
            return cleaned
        raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text}")

    def solve_single_choice(self, question: str, options: list, company: str = "Target Employer", job_title: str = "Senior Software Engineer") -> dict:
        """Handles dropdown / radio button questions."""
        ctx = self.context_manager.build_context_for_question(question, company, job_title)
        options_list_str = "\n".join([f"- {opt}" for opt in options])

        system_prompt = (
            "You are an AI decision engine for a candidate applying to a job.\n"
            "Your task is to select EXACTLY ONE option from the provided list that truthfully represents the candidate.\n"
            "Rules:\n"
            "1. Output MUST be valid JSON: {\"answer\": \"<exact option text>\", \"evidence\": \"<short reason>\"}\n"
            "2. The \"answer\" field MUST match one of the provided options verbatim.\n"
            "3. If question is about visa sponsorship: Candidate does NOT need sponsorship for US roles; has legal US authorization.\n"
            "4. If question asks if candidate accepts/meets salary range: ALWAYS choose Yes.\n"
            "5. If question asks if candidate has previously worked for the company, tribe, or government: choose No if not listed in profile.\n"
            "6. Answer directly in JSON. Do not write explanations outside JSON."
        )

        user_prompt = f"""CANDIDATE CONTEXT:
{json.dumps(ctx, indent=2)}

QUESTION:
{question}

OPTIONS:
{options_list_str}

Select the single best option matching the candidate profile."""

        raw_output = self._call_ollama(user_prompt, system_prompt=system_prompt, temperature=0.0, max_tokens=350)

        # Parse JSON output
        parsed_answer = ""
        evidence = ""
        try:
            match = re.search(r"\{.*?\}", raw_output, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                parsed_answer = data.get("answer", "").strip()
                evidence = data.get("evidence", "").strip()
        except Exception:
            pass

        # Fallback exact matching against options
        chosen_option = self._match_best_option(parsed_answer or raw_output, options)

        return {
            "type": "single_choice",
            "answer": chosen_option,
            "evidence": evidence,
            "raw_llm_output": raw_output
        }

    def solve_multi_choice(self, question: str, options: list, company: str = "Target Employer", job_title: str = "Senior Software Engineer") -> dict:
        """Handles checkboxes / multi-select questions."""
        ctx = self.context_manager.build_context_for_question(question, company, job_title)
        options_list_str = "\n".join([f"- {opt}" for opt in options])

        system_prompt = (
            "You are an AI decision engine for a job applicant.\n"
            "Select ALL options that truthfully apply to the candidate based on their profile.\n"
            "Output MUST be valid JSON: {\"selected\": [\"<option1>\", \"<option2>\"], \"evidence\": \"<short reason>\"}\n"
            "Only select an option if the candidate profile clearly supports it. If none apply, select 'None of the above' if available.\n"
            "Output ONLY JSON, nothing else."
        )

        user_prompt = f"""CANDIDATE CONTEXT:
{json.dumps(ctx, indent=2)}

QUESTION:
{question}

OPTIONS (Choose all that apply):
{options_list_str}"""

        raw_output = self._call_ollama(user_prompt, system_prompt=system_prompt, temperature=0.0, max_tokens=150)

        selected = []
        evidence = ""
        try:
            match = re.search(r"\{.*\}", raw_output, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                raw_selected = data.get("selected", [])
                evidence = data.get("evidence", "").strip()
                for item in raw_selected:
                    matched = self._match_best_option(item, options)
                    if matched and matched not in selected:
                        selected.append(matched)
        except Exception:
            pass

        if not selected:
            # Fallback check
            for opt in options:
                if opt.lower() in raw_output.lower():
                    selected.append(opt)

        return {
            "type": "multi_choice",
            "selected": selected,
            "evidence": evidence,
            "raw_llm_output": raw_output
        }

    def solve_open_ended(self, question: str, company: str = "Target Employer", job_title: str = "Senior Software Engineer") -> dict:
        """Handles open-ended text answers (1-3 sentences, first-person)."""
        ctx = self.context_manager.build_context_for_question(question, company, job_title)

        system_prompt = (
            "You are the candidate applying for this job. Answer in the first person ('I', 'my').\n"
            "Rules:\n"
            "1. Keep the answer professional, authentic, direct, and concise (1 to 3 sentences).\n"
            "2. Never say 'As an AI' or 'Based on the profile'.\n"
            "3. Ground all statements in the candidate's actual work experience and skills.\n"
            "4. For questions asking why you want to join this company, express genuine enthusiasm for their domain and align it with your background in distributed systems / AI.\n"
            "5. If asked for a URL not in profile (e.g. personal website), answer 'N/A'."
        )

        user_prompt = f"""CANDIDATE CONTEXT:
{json.dumps(ctx, indent=2)}

QUESTION:
{question}

Answer:"""

        raw_output = self._call_ollama(user_prompt, system_prompt=system_prompt, temperature=0.3, max_tokens=400)
        cleaned = raw_output.strip().strip('"')

        return {
            "type": "open_ended_text",
            "answer": cleaned,
            "raw_llm_output": raw_output
        }

    def solve_numeric(self, question: str, options: list = None, company: str = "Target Employer") -> dict:
        """Handles questions asking for a specific number or year amount."""
        if options:
            return self.solve_single_choice(question, options, company)

        ctx = self.context_manager.build_context_for_question(question, company)
        system_prompt = "Return ONLY the numerical value or concise amount (e.g. '8', '130000'). No explanation."
        user_prompt = f"Candidate Context:\n{json.dumps(ctx['layer_1_core'], indent=2)}\n\nQuestion: {question}"

        raw_output = self._call_ollama(user_prompt, system_prompt=system_prompt, temperature=0.0, max_tokens=30)
        digits = re.search(r"\d+", raw_output)
        val = digits.group(0) if digits else raw_output.strip()

        return {
            "type": "numeric",
            "answer": val,
            "raw_llm_output": raw_output
        }

    def _match_best_option(self, raw_str: str, options: list) -> str:
        """Finds closest option from options list to avoid minor phrasing deviations."""
        if not options:
            return raw_str

        raw_clean = raw_str.strip().lower()

        # 1. Exact case-insensitive match
        for opt in options:
            if opt.strip().lower() == raw_clean:
                return opt

        # 2. Substring match
        for opt in options:
            if opt.strip().lower() in raw_clean or raw_clean in opt.strip().lower():
                return opt

        # 3. Keyword / sentiment fallback
        if "yes" in raw_clean:
            for opt in options:
                if "yes" in opt.lower():
                    return opt
        if "no" in raw_clean:
            for opt in options:
                if "no" in opt.lower():
                    return opt

        return options[0]


def run_interactive_mode(solver: OllamaQuestionSolver):
    """Interactive loop for testing any question & options pasted by the user."""
    print("\n" + "=" * 65)
    print(" OLLAMA QUESTION SOLVER - INTERACTIVE TEST")
    print(f" Model: {solver.model} | Host: {solver.host}")
    print(" Candidate: Jimmy Tran (8+ years exp, US Authorized, .NET/React/AI/Python)")
    print("=" * 65)
    print("Type your question below (or 'exit' to quit).\n")

    while True:
        try:
            print("-" * 65)
            question = input("\n[?] Enter Question: ").strip()
            if not question:
                continue
            if question.lower() in ("exit", "quit", "q"):
                print("Exiting interactive test. Goodbye!")
                break

            company = input("[?] Company Name (default: 'General Tech'): ").strip() or "General Tech"

            print("[?] Enter Options (comma-separated, OR paste multiple lines and press Enter twice; or leave empty for text):")
            options = []
            first_line = input("    > ").strip()
            if first_line:
                if "," in first_line:
                    options = [opt.strip() for opt in first_line.split(",") if opt.strip()]
                else:
                    options.append(first_line)
                    while True:
                        line = input("    > ").strip()
                        if not line:
                            break
                        options.append(line)

            # Determine field type
            if not options:
                print("\n[->] Detected Type: OPEN-ENDED TEXT")
                start_t = time.time()
                result = solver.solve_open_ended(question, company=company)
                dur = round(time.time() - start_t, 2)

                print("\n" + "#" * 45)
                print(f"[ANSWER] ({dur}s):")
                print(result["answer"])
                print("#" * 45)

            else:
                mode_choice = input("[?] Is this multi-select checkboxes? (y/N): ").strip().lower()
                start_t = time.time()
                if mode_choice == "y":
                    print("\n[->] Detected Type: MULTI-CHOICE (Checkboxes)")
                    result = solver.solve_multi_choice(question, options, company=company)
                    dur = round(time.time() - start_t, 2)

                    print("\n" + "#" * 45)
                    print(f"[SELECTED CHECKBOXES] ({dur}s):")
                    for s in result.get("selected", []):
                        print(f"  [X] {s}")
                    if result.get("evidence"):
                        print(f"\n[Evidence]: {result['evidence']}")
                    print("#" * 45)
                else:
                    print("\n[->] Detected Type: SINGLE-CHOICE (Dropdown / Radio)")
                    result = solver.solve_single_choice(question, options, company=company)
                    dur = round(time.time() - start_t, 2)

                    print("\n" + "#" * 45)
                    print(f"[CHOSEN OPTION] ({dur}s): {result['answer']}")
                    if result.get("evidence"):
                        print(f"[Evidence]: {result['evidence']}")
                    print("#" * 45)

        except KeyboardInterrupt:
            print("\nExiting.")
            break
        except Exception as e:
            print(f"[!] Error: {e}")


def run_batch_tests(solver: OllamaQuestionSolver, json_file: str = None):
    """Runs automated batch tests using sample questions from actual job forms."""
    test_file = json_file or os.path.join(CURRENT_DIR, "sample_questions.json")
    if not os.path.exists(test_file):
        print(f"[!] Test file not found: {test_file}")
        return

    with open(test_file, "r", encoding="utf-8") as f:
        tests = json.load(f)

    print("\n" + "=" * 70)
    print(f" RUNNING BATCH TEST SUITE ({len(tests)} questions from real application forms)")
    print(f" Model: {solver.model} | Host: {solver.host}")
    print("=" * 70)

    for idx, t in enumerate(tests, 1):
        q = t["question"]
        q_type = t.get("type", "single_choice")
        options = t.get("options", [])
        company = t.get("company", "Company")

        print(f"\n[{idx}/{len(tests)}] [{company}] {q}")
        start_t = time.time()

        if q_type == "multi_choice":
            res = solver.solve_multi_choice(q, options, company)
            dur = round(time.time() - start_t, 2)
            print(f"      Selected ({dur}s): {res.get('selected')}")
            if res.get("evidence"):
                print(f"      Evidence: {res['evidence']}")
        elif q_type == "text":
            res = solver.solve_open_ended(q, company)
            dur = round(time.time() - start_t, 2)
            print(f"      Response ({dur}s): {res.get('answer')}")
        else:
            res = solver.solve_single_choice(q, options, company)
            dur = round(time.time() - start_t, 2)
            print(f"      Answer ({dur}s): {res.get('answer')}")
            if res.get("evidence"):
                print(f"      Evidence: {res['evidence']}")

    print("\n" + "=" * 70)
    print(" BATCH TESTS COMPLETED")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Test Ollama Question Solver with Hierarchical Context")
    parser.add_argument("-b", "--batch", action="store_true", help="Run automated batch test on sample job questions")
    parser.add_argument("-f", "--file", type=str, help="Path to custom JSON file with questions")
    parser.add_argument("-m", "--model", type=str, help="Specific Ollama model name to use")
    parser.add_argument("-i", "--interactive", action="store_true", help="Run interactive paste-and-test loop")

    args = parser.parse_args()

    solver = OllamaQuestionSolver(model=args.model)

    if args.batch or args.file:
        run_batch_tests(solver, json_file=args.file)
    else:
        # Default to interactive mode
        run_interactive_mode(solver)


if __name__ == "__main__":
    main()
