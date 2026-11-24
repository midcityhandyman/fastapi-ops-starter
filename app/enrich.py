"""Rule based triage for a normalised record. Pure functions, no I/O."""

from __future__ import annotations

import re

URGENT_PATTERNS = [
    r"\burgent\b",
    r"\bemergency\b",
    r"\basap\b",
    r"\bleak(ing|s)?\b",
    r"\bflood(ed|ing)?\b",
    r"\bno (heat|power|water)\b",
    r"\bright away\b",
]

TAG_PATTERNS = {
    "quote": [r"\bquote\b", r"\bestimate\b", r"\bpricing\b", r"\bhow much\b"],
    "scheduling": [r"\breschedul", r"\bappointment\b", r"\bavailab", r"\bbook(ing)?\b"],
    "billing": [r"\binvoice\b", r"\bpayment\b", r"\brefund\b", r"\bcharged?\b", r"\breceipt\b"],
    "complaint": [r"\bunhappy\b", r"\bcomplain", r"\bdisappointed\b", r"\bnot acceptable\b"],
}

CALLBACK_EVENTS = {"call_missed", "voicemail_left"}


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def classify(event_type: str, summary: str) -> tuple[str, list[str]]:
    """Return (priority, tags) for a record.

    Priority is ``high`` when the text sounds urgent, when it is a complaint,
    or when a call went unanswered. Everything else is ``normal``.
    """
    text = (summary or "").lower()
    tags = [tag for tag, patterns in TAG_PATTERNS.items() if _matches(patterns, text)]
    urgent = _matches(URGENT_PATTERNS, text)
    if urgent:
        tags.append("urgent")
    if event_type in CALLBACK_EVENTS:
        tags.append("callback")
    high = urgent or "complaint" in tags or event_type in CALLBACK_EVENTS
    return ("high" if high else "normal"), sorted(tags)
