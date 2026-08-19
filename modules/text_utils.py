import re


def strip_markdown_formatting(text: str) -> str:
    """Safety net in case Ollama ignores the plain-prose instruction: strips common markdown syntax."""
    if not text:
        return text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)  # __bold__
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)  # *italic*
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)  # # Headers
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)  # - bullet points
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)  # 1. numbered lists
    return text.strip()
