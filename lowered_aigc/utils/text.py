from __future__ import annotations

import re
from difflib import SequenceMatcher


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(
    r"[\s\u3000，。！？；：、“”‘’（）《》〈〉【】\[\]{}()<>.,!?;:'\"`~\-_=+*/\\|@#$%^&]+"
)


def clean_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = text.replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text)
    return text.strip()


def normalize_for_match(text: str) -> str:
    return _PUNCT_RE.sub("", text).lower()


def sentence_split(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])", text)
    return [part.strip() for part in parts if part.strip()]


def similarity(a: str, b: str) -> float:
    na = normalize_for_match(a)
    nb = normalize_for_match(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def partial_ratio(short_text: str, long_text: str) -> float:
    short = normalize_for_match(short_text)
    long = normalize_for_match(long_text)
    if not short or not long:
        return 0.0
    if len(short) > len(long):
        short, long = long, short
    if short in long:
        return 1.0
    if len(short) < 12:
        return SequenceMatcher(None, short, long).ratio()

    window = min(len(long), max(len(short), int(len(short) * 1.35)))
    step = max(8, len(short) // 4)
    best = 0.0
    for start in range(0, max(1, len(long) - window + 1), step):
        chunk = long[start : start + window]
        best = max(best, SequenceMatcher(None, short, chunk).ratio())
        if best >= 0.98:
            break
    if len(long) > window:
        best = max(best, SequenceMatcher(None, short, long[-window:]).ratio())
    return best


def compact_snippet(text: str, limit: int = 120) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"

