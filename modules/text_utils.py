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


def normalize_company(name: str) -> str:
    """Normalizes company names for deduplication across different jobs."""
    if not name:
        return ""
    c = str(name).strip().lower()
    # Strip punctuation and special characters
    c = re.sub(r'[\.,\-_/\\()&]', ' ', c)
    # Remove common corporate designations
    c = re.sub(r'\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|group|technologies|tech|solutions|services|holdings|enterprises|pvt|plc)\b', '', c)
    # Collapse multiple whitespaces
    c = re.sub(r'\s+', ' ', c).strip()
    return c if c else str(name).strip().lower()
