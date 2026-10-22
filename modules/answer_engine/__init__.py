"""Grounded, never-blank answer engine (v2). Enabled with ANSWER_ENGINE=v2 in config/.env."""
import config

from .engine import AnswerEngine
from .result import AnswerResult


def engine_settings() -> dict:
    """Settings snapshot. The model and host always come from the bot's existing config."""
    think = bool(config.ANSWER_THINK)
    return {"host": config.OLLAMA_HOST, "model": config.OLLAMA_MODEL, "num_ctx": config.ANSWER_NUM_CTX,
            "temperature": config.ANSWER_TEMPERATURE, "keep_alive": config.ANSWER_KEEP_ALIVE, "think": think,
            "num_predict": max(config.ANSWER_NUM_PREDICT, 800) if think else config.ANSWER_NUM_PREDICT,
            "max_attempts": max(1, min(2, config.ANSWER_MAX_ATTEMPTS)), "facts_source": config.ANSWER_FACTS_SOURCE,
            "resume_data_dir": config.ANSWER_RESUME_DATA_DIR, "audit": config.ANSWER_AUDIT}


def v2_enabled() -> bool:
    return config.ANSWER_ENGINE == "v2"


__all__ = ["AnswerEngine", "AnswerResult", "engine_settings", "v2_enabled"]
