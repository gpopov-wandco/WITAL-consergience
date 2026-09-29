"""PII redaction for exports (F27, F25): phones, e-mails, IBANs, known names.

Implements: F27, F25, C3
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<![\w/])(?:\+|00)?\d[\d\s().-]{6,}\d(?![\w/])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b")


def redact(text: str, *, names: Iterable[str] = ()) -> str:
    """Replace personal identifiers with placeholders; known names are removed whole."""
    result = _EMAIL.sub("[e-mail]", text)
    result = _IBAN.sub("[iban]", result)
    result = _PHONE.sub("[phone]", result)
    for name in names:
        cleaned = name.strip()
        if len(cleaned) < 2:
            continue
        parts = [cleaned, *[p for p in cleaned.split() if p != cleaned]]
        for part in parts:  # the whole name first, then each part
            if len(part) >= 2:
                result = re.sub(rf"(?<!\w){re.escape(part)}(?!\w)", "[name]", result, flags=re.I)
    return result
