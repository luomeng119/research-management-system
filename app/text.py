from __future__ import annotations

import unicodedata


def _is_ignorable(character: str) -> bool:
    """Treat whitespace, separators and control/format marks as visually empty."""
    return character.isspace() or unicodedata.category(character)[0] in {"C", "Z"}


def normalize_text(value: str) -> str:
    """Normalize text and trim visually empty Unicode characters at its edges."""
    normalized = unicodedata.normalize("NFKC", value)
    start = 0
    end = len(normalized)
    while start < end and _is_ignorable(normalized[start]):
        start += 1
    while end > start and _is_ignorable(normalized[end - 1]):
        end -= 1
    return normalized[start:end]


def has_meaningful_text(value: object) -> bool:
    """Return true only when normalized text contains a visible/content character."""
    if not isinstance(value, str):
        return False
    return any(
        unicodedata.category(character)[0] in {"L", "N", "P", "S"}
        for character in unicodedata.normalize("NFKC", value)
    )
