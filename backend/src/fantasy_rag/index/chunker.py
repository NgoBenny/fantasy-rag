"""Split a story (title + summary) into ~300-word chunks on sentence boundaries."""

import re

MAX_WORDS = 300  # ~300 tokens for bge-small (512-token limit), per PLAN


def chunk(title: str, summary: str, max_words: int = MAX_WORDS) -> list[str]:
    text = f"{title}. {summary}".strip(" .") if summary else title
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current: list[str] = []
    for s in sentences:
        words = s.split()
        if current and len(current) + len(words) > max_words:
            chunks.append(" ".join(current))
            current = []
        # A single over-long sentence is cut by words.
        while len(words) > max_words:
            chunks.append(" ".join(words[:max_words]))
            words = words[max_words:]
        current.extend(words)
    if current:
        chunks.append(" ".join(current))
    return chunks
