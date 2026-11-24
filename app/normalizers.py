"""Turn source specific payloads into one normalised event shape.

Each source has a pydantic model describing what it sends and a small
function that maps it onto ``NormalizedEvent``. Adding a source means adding
one model, one function and one line in ``NORMALIZERS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SUMMARY_LIMIT = 500


class UnknownSourceError(LookupError):
    pass


class NormalizationError(ValueError):
    """The payload does not match what the source is expected to send."""

    def __init__(self, source: str, problems: list[str]):
        super().__init__(f"invalid {source} payload: " + "; ".join(problems))
        self.source = source
        self.problems = problems


@dataclass(frozen=True)
class NormalizedEvent:
    source: str
    external_id: str
    event_type: str
    occurred_at: datetime
    contact_name: str | None
    contact_phone: str | None
    contact_email: str | None
    summary: str


# ---------------------------------------------------------------- helpers


def parse_timestamp(value: Any) -> datetime:
    """Accept epoch seconds, epoch milliseconds, ISO 8601 and RFC 2822."""
    if isinstance(value, bool) or value is None:
        raise ValueError("timestamp is missing")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            # A number no calendar can hold is bad input, not a server error.
            raise ValueError(f"timestamp out of range: {value!r}") from exc
    text = str(value).strip()
    if not text:
        raise ValueError("timestamp is empty")
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return parse_timestamp(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"timestamp not understood: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"timestamp out of range: {text!r}") from exc


def normalize_phone(value: str | None) -> str | None:
    """Return a +E.164 style number, or None when it is not a usable number."""
    if not value:
        return None
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    return text if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text) else None


def clean_text(value: str | None, limit: int = SUMMARY_LIMIT) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def clean_name(value: str | None) -> str | None:
    return clean_text(value, 120) or None


# ---------------------------------------------------------------- sources


class _Payload(BaseModel):
    # Unknown fields are kept in the raw column, they are not an error.
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class WebformAnswers(_Payload):
    full_name: str | None = None
    phone: str | None = None
    email: str | None = None
    message: str = ""


class WebformPayload(_Payload):
    submission_id: str | int
    submitted_at: str | int | float
    form: str = "contact"
    answers: WebformAnswers


class SmsPayload(_Payload):
    message_sid: str
    sender: str = Field(alias="from")
    body: str = ""
    timestamp: str | int | float


class CallPayload(_Payload):
    call_id: str | int
    caller_number: str | None = None
    caller_name: str | None = None
    started_at: str | int | float
    duration_seconds: int = Field(default=0, ge=0)
    outcome: str = "answered"
    transcript: str | None = None


class EmailPayload(_Payload):
    message_id: str
    sender: str = Field(alias="from")
    subject: str = ""
    text: str = ""
    date: str | int | float


def _webform(p: WebformPayload) -> NormalizedEvent:
    form = clean_text(p.form, 40).lower() or "contact"
    message = clean_text(p.answers.message)
    return NormalizedEvent(
        source="webform",
        external_id=str(p.submission_id),
        event_type="form_submission",
        occurred_at=parse_timestamp(p.submitted_at),
        contact_name=clean_name(p.answers.full_name),
        contact_phone=normalize_phone(p.answers.phone),
        contact_email=normalize_email(p.answers.email),
        summary=clean_text(f"[{form} form] {message}"),
    )


def _sms(p: SmsPayload) -> NormalizedEvent:
    return NormalizedEvent(
        source="sms",
        external_id=p.message_sid,
        event_type="message_received",
        occurred_at=parse_timestamp(p.timestamp),
        contact_name=None,
        contact_phone=normalize_phone(p.sender),
        contact_email=None,
        summary=clean_text(p.body),
    )


_CALL_EVENTS = {
    "answered": "call_answered",
    "missed": "call_missed",
    "voicemail": "voicemail_left",
}


def _call(p: CallPayload) -> NormalizedEvent:
    outcome = p.outcome.strip().lower()
    if outcome not in _CALL_EVENTS:
        raise ValueError(f"outcome must be one of {sorted(_CALL_EVENTS)}, got {p.outcome!r}")
    if p.transcript:
        summary = clean_text(p.transcript)
    elif outcome == "missed":
        summary = "Missed call, no voicemail."
    else:
        summary = f"Call {outcome}, {p.duration_seconds} seconds, no transcript."
    return NormalizedEvent(
        source="call",
        external_id=str(p.call_id),
        event_type=_CALL_EVENTS[outcome],
        occurred_at=parse_timestamp(p.started_at),
        contact_name=clean_name(p.caller_name),
        contact_phone=normalize_phone(p.caller_number),
        contact_email=None,
        summary=summary,
    )


def _email(p: EmailPayload) -> NormalizedEvent:
    display_name, address = parseaddr(p.sender)
    subject = clean_text(p.subject, 150)
    body = clean_text(p.text)
    return NormalizedEvent(
        source="email",
        external_id=p.message_id.strip().strip("<>"),
        event_type="email_received",
        occurred_at=parse_timestamp(p.date),
        contact_name=clean_name(display_name),
        contact_phone=None,
        contact_email=normalize_email(address),
        summary=clean_text(f"{subject}: {body}" if subject and body else subject or body),
    )


NORMALIZERS: dict[str, tuple[type[_Payload], Callable[[Any], NormalizedEvent]]] = {
    "webform": (WebformPayload, _webform),
    "sms": (SmsPayload, _sms),
    "call": (CallPayload, _call),
    "email": (EmailPayload, _email),
}


def known_sources() -> list[str]:
    return sorted(NORMALIZERS)


def normalize(source: str, payload: Any) -> NormalizedEvent:
    """Validate ``payload`` for ``source`` and return the normalised event."""
    entry = NORMALIZERS.get(source)
    if entry is None:
        raise UnknownSourceError(source)
    model, mapper = entry
    if not isinstance(payload, dict):
        raise NormalizationError(source, ["payload must be a JSON object"])
    try:
        parsed = model.model_validate(payload)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in err['loc']) or 'payload'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise NormalizationError(source, problems) from exc
    try:
        event = mapper(parsed)
    except ValueError as exc:
        raise NormalizationError(source, [str(exc)]) from exc
    if not event.external_id.strip():
        raise NormalizationError(source, ["external id is empty"])
    return event
