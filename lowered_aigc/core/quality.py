from __future__ import annotations

from ..utils.text import similarity


def validate_rewrite(original: str, rewritten: str, masks: dict[str, str]) -> list[str]:
    return validate_rewrite_with_thresholds(original, rewritten, masks)


def validate_rewrite_with_thresholds(
    original: str,
    rewritten: str,
    masks: dict[str, str],
    *,
    min_length_ratio: float = 0.45,
    max_length_ratio: float = 1.8,
    max_similarity: float = 0.985,
) -> list[str]:
    issues: list[str] = []
    stripped = rewritten.strip()
    if not stripped:
        issues.append("empty_rewrite")
    if len(stripped) < max(10, int(len(original) * min_length_ratio)):
        issues.append("too_short")
    if len(stripped) > max(80, int(len(original) * max_length_ratio)):
        issues.append("too_long")
    # External model/API skills should usually produce a stronger rewrite, but
    # the bundled offline baseline is deliberately conservative. Keep this gate
    # focused on exact or near-exact no-op rewrites.
    if similarity(original, stripped) > max_similarity and len(original) > 80:
        issues.append("too_similar")
    for token, value in masks.items():
        if value and value not in stripped:
            issues.append(f"mask_missing:{token}")
    return issues
