from datetime import datetime, timezone

import pytest

from app.enrich import classify
from app.normalizers import (
    NormalizationError,
    UnknownSourceError,
    known_sources,
    normalize,
    normalize_email,
    normalize_phone,
    parse_timestamp,
)


def test_known_sources():
    assert known_sources() == ["call", "email", "sms", "webform"]


def test_webform_is_normalised(webform_payload):
    event = normalize("webform", webform_payload)
    assert event.source == "webform"
    assert event.external_id == "wf-test-1"
    assert event.event_type == "form_submission"
    assert event.occurred_at == datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
    assert event.contact_name == "Alex Sample"
    assert event.contact_phone == "+12025550101"
    assert event.contact_email == "alex.sample@example.com"
    assert event.summary == "[quote form] Please send a quote for two doors."


def test_sms_uses_alias_and_epoch_seconds():
    event = normalize(
        "sms",
        {"message_sid": "SM1", "from": "202-555-0111", "body": " hello\n there ", "timestamp": 1793628000},
    )
    assert event.event_type == "message_received"
    assert event.contact_phone == "+12025550111"
    assert event.summary == "hello there"
    assert event.occurred_at == datetime.fromtimestamp(1793628000, tz=timezone.utc)


@pytest.mark.parametrize(
    "outcome, event_type",
    [("answered", "call_answered"), ("missed", "call_missed"), ("Voicemail", "voicemail_left")],
)
def test_call_outcomes(outcome, event_type):
    event = normalize(
        "call",
        {"call_id": 7, "caller_number": "anonymous", "started_at": "2026-11-03T12:30:10Z", "outcome": outcome},
    )
    assert event.external_id == "7"
    assert event.event_type == event_type
    assert event.contact_phone is None
    assert event.summary


def test_call_rejects_unknown_outcome():
    with pytest.raises(NormalizationError) as info:
        normalize("call", {"call_id": 1, "started_at": "2026-11-03T12:30:10Z", "outcome": "exploded"})
    assert "outcome" in info.value.problems[0]


def test_email_parses_sender_and_rfc2822_date():
    event = normalize(
        "email",
        {
            "message_id": "<abc@mail.example.com>",
            "from": '"Quinn Dummy" <Quinn.Dummy@Example.com>',
            "subject": "Hello",
            "text": "Body text",
            "date": "Wed, 04 Nov 2026 16:42:00 -0500",
        },
    )
    assert event.external_id == "abc@mail.example.com"
    assert event.contact_name == "Quinn Dummy"
    assert event.contact_email == "quinn.dummy@example.com"
    assert event.summary == "Hello: Body text"
    assert event.occurred_at == datetime(2026, 11, 4, 21, 42, tzinfo=timezone.utc)


def test_missing_fields_are_listed():
    with pytest.raises(NormalizationError) as info:
        normalize("webform", {"form": "contact"})
    joined = " ".join(info.value.problems)
    assert "submission_id" in joined and "submitted_at" in joined and "answers" in joined


def test_bad_timestamp_is_a_normalization_error(webform_payload):
    webform_payload["submitted_at"] = "next tuesday"
    with pytest.raises(NormalizationError):
        normalize("webform", webform_payload)


def test_non_object_payload_and_unknown_source():
    with pytest.raises(NormalizationError):
        normalize("sms", ["not", "an", "object"])
    with pytest.raises(UnknownSourceError):
        normalize("carrier-pigeon", {})


def test_long_summary_is_truncated():
    event = normalize("sms", {"message_sid": "SM2", "from": "+12025550111", "body": "x" * 2000, "timestamp": 1})
    assert len(event.summary) == 500 and event.summary.endswith("...")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("(202) 555-0101", "+12025550101"),
        ("1-202-555-0101", "+12025550101"),
        ("+44 20 7946 0958", "+442079460958"),
        ("555-0101", None),
        ("not provided", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_normalize_email():
    assert normalize_email(" A@Example.COM ") == "a@example.com"
    assert normalize_email("nope") is None


def test_parse_timestamp_variants():
    expected = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
    epoch = int(expected.timestamp())
    assert parse_timestamp(epoch) == expected
    assert parse_timestamp(epoch * 1000) == expected
    assert parse_timestamp(str(epoch)) == expected
    assert parse_timestamp("2026-11-02T00:00:00Z") == expected
    assert parse_timestamp("2026-11-02T00:00:00") == expected  # naive means UTC
    with pytest.raises(ValueError):
        parse_timestamp(True)


@pytest.mark.parametrize(
    "event_type, summary, priority, tags",
    [
        ("form_submission", "Looking for a quote on doors", "normal", ["quote"]),
        ("message_received", "EMERGENCY no heat in the house", "high", ["urgent"]),
        ("call_missed", "Missed call, no voicemail.", "high", ["callback"]),
        ("email_received", "I am unhappy about the invoice", "high", ["billing", "complaint"]),
        ("message_received", "See you then", "normal", []),
    ],
)
def test_classify(event_type, summary, priority, tags):
    assert classify(event_type, summary) == (priority, tags)


@pytest.mark.parametrize("value", [1e30, -1e30, float("inf"), float("nan"), "99999999999999999999999", "9999-12-31T23:59:59-23:00"])
def test_out_of_range_timestamp_is_rejected_not_a_crash(value):
    with pytest.raises(ValueError):
        parse_timestamp(value)
    with pytest.raises(NormalizationError):
        normalize("sms", {"message_sid": "SM9", "from": "+12025550111", "body": "x", "timestamp": value})
