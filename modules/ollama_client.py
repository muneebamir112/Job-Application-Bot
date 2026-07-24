import requests
import json
import time
import config
from modules.logger import logger

def query_ollama(prompt: str, system_prompt: str = "", timeout: int = None) -> str:
    """
    Sends a generation prompt to the local Ollama API.
    Handles retries and connection failures gracefully.
    """
    if timeout is None:
        timeout = config.OLLAMA_TIMEOUT
    url = f"{config.OLLAMA_HOST}/api/generate"
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False
    }
    if system_prompt:
        payload["system"] = system_prompt

    for attempt in range(config.MAX_RETRIES):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            if response.status_code == 200:
                result = response.json()
                return result.get("response", "").strip()
            else:
                logger.warning(f"Ollama returned status code {response.status_code}. Attempt {attempt + 1} of {config.MAX_RETRIES}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Ollama connection attempt {attempt + 1} failed: {e}")
        time.sleep(2)

    raise ConnectionError(
        f"Could not connect to Ollama model '{config.OLLAMA_MODEL}' at {config.OLLAMA_HOST}. "
        "Please make sure the Ollama application is running ('ollama serve') and the model is pulled ('ollama pull <model>')."
    )
