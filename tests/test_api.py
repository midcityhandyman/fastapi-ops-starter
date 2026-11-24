import json
import logging

from app.logging_setup import JsonFormatter, request_id_var
from tests.conftest import AUTH, TEST_KEY


# ---------------------------------------------------------------- auth


def test_missing_key_is_401(client):
    response = client.get("/v1/records")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "ApiKey"


def test_wrong_key_is_401(client):
    assert client.get("/v1/records", headers={"X-API-Key": "wrong-key-entirely"}).status_code == 401


def test_bearer_token_is_accepted(client):
    assert client.get("/v1/records", headers={"Authorization": f"Bearer {TEST_KEY}"}).status_code == 200


def test_every_v1_route_requires_a_key(app, client):
    paths = {path: ops for path, ops in app.openapi()["paths"].items() if path.startswith("/v1")}
    assert len(paths) >= 10
    for path, operations in paths.items():
        url = path.replace("{source}", "sms").replace("{record_id}", "x").replace("{job_id}", "x")
        for method in operations:
            assert client.request(method.upper(), url).status_code == 401, f"{method} {path}"


# ---------------------------------------------------------------- health


def test_healthz_needs_no_key(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_ready(client):
    response = client.get("/readyz")
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "ready"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["migrations"] == {"ok": True, "pending": []}
    assert body["checks"]["worker"] == {"ok": True, "enabled": False}


# ---------------------------------------------------------------- ingest


def test_ingest_creates_record_and_job(client, webform_payload):
    response = client.post("/v1/ingest/webform", json=webform_payload, headers=AUTH)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "created"
    assert body["job_id"]
    record = body["record"]
    assert record["contact"] == {
        "name": "Alex Sample",
        "phone": "+12025550101",
        "email": "alex.sample@example.com",
    }
    assert record["occurred_at"] == "2026-11-10T14:00:00+00:00"
    assert record["priority"] == "normal" and record["enriched_at"] is None

    detail = client.get(f"/v1/records/{record['id']}", headers=AUTH).json()
    assert detail["raw"] == webform_payload
    assert [job["kind"] for job in detail["jobs"]] == ["enrich_record"]
    assert detail["jobs"][0]["status"] == "queued"


def test_replay_is_idempotent(client, webform_payload):
    first = client.post("/v1/ingest/webform", json=webform_payload, headers=AUTH)
    second = client.post("/v1/ingest/webform", json=webform_payload, headers=AUTH)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["status"] == "duplicate"
    assert second.json()["record"]["id"] == first.json()["record"]["id"]
    assert second.json()["job_id"] is None
    stats = client.get("/v1/stats", headers=AUTH).json()
    assert stats["records"]["total"] == 1
    assert stats["jobs"]["by_status"] == {"queued": 1}


def test_same_external_id_on_two_sources_is_two_records(client):
    client.post(
        "/v1/ingest/sms",
        json={"message_sid": "shared-1", "from": "+12025550111", "body": "a", "timestamp": 1},
        headers=AUTH,
    )
    client.post(
        "/v1/ingest/call",
        json={"call_id": "shared-1", "started_at": "2026-11-03T12:30:10Z", "outcome": "missed"},
        headers=AUTH,
    )
    assert client.get("/v1/stats", headers=AUTH).json()["records"]["by_source"] == {"call": 1, "sms": 1}


def test_invalid_payload_is_422_and_stores_nothing(client):
    response = client.post("/v1/ingest/sms", json={"body": "no ids"}, headers=AUTH)
    assert response.status_code == 422
    problems = response.json()["detail"]["problems"]
    assert any("message_sid" in problem for problem in problems)
    assert client.get("/v1/stats", headers=AUTH).json()["records"]["total"] == 0


def test_unknown_source_is_404(client):
    assert client.post("/v1/ingest/fax", json={}, headers=AUTH).status_code == 404
    assert client.get("/v1/sources", headers=AUTH).json() == {"sources": ["call", "email", "sms", "webform"]}


def test_batch_reports_each_event(client):
    events = [
        {"message_sid": "b1", "from": "+12025550111", "body": "one", "timestamp": 100},
        {"message_sid": "b1", "from": "+12025550111", "body": "one again", "timestamp": 100},
        {"from": "+12025550111"},
        {"message_sid": "b2", "from": "+12025550112", "body": "two", "timestamp": 200},
    ]
    response = client.post("/v1/ingest/sms/batch", json={"events": events}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {"created": 2, "duplicate": 1, "rejected": 1}
    assert [item["status"] for item in body["results"]] == ["created", "duplicate", "rejected", "created"]


# ---------------------------------------------------------------- records


def _load_three(client):
    client.post(
        "/v1/ingest/sms",
        json={"message_sid": "r1", "from": "+12025550111", "body": "need a quote", "timestamp": "2026-11-01T10:00:00Z"},
        headers=AUTH,
    )
    client.post(
        "/v1/ingest/sms",
        json={"message_sid": "r2", "from": "+12025550112", "body": "URGENT leak", "timestamp": "2026-11-02T10:00:00Z"},
        headers=AUTH,
    )
    client.post(
        "/v1/ingest/call",
        json={"call_id": "r3", "started_at": "2026-11-03T10:00:00Z", "outcome": "missed"},
        headers=AUTH,
    )


def test_list_records_orders_filters_and_pages(client, worker):
    _load_three(client)
    worker.drain()

    everything = client.get("/v1/records", headers=AUTH).json()
    assert everything["total"] == 3
    assert [item["external_id"] for item in everything["items"]] == ["r3", "r2", "r1"]

    assert client.get("/v1/records?source=sms", headers=AUTH).json()["total"] == 2
    assert client.get("/v1/records?event_type=call_missed", headers=AUTH).json()["total"] == 1
    assert client.get("/v1/records?priority=high", headers=AUTH).json()["total"] == 2
    assert client.get("/v1/records?q=urgent", headers=AUTH).json()["items"][0]["external_id"] == "r2"
    since = client.get("/v1/records", params={"since": "2026-11-02T00:00:00Z"}, headers=AUTH).json()
    assert since["total"] == 2

    page = client.get("/v1/records?limit=1&offset=1", headers=AUTH).json()
    assert page["total"] == 3 and [item["external_id"] for item in page["items"]] == ["r2"]

    assert client.get("/v1/records?since=whenever", headers=AUTH).status_code == 422
    assert client.get("/v1/records?limit=0", headers=AUTH).status_code == 422
    assert client.get("/v1/records/nope", headers=AUTH).status_code == 404


# ---------------------------------------------------------------- logging


def test_request_id_is_echoed_or_generated(client):
    echoed = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["x-request-id"] == "abc-123"
    generated = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert len(generated.headers["x-request-id"]) == 32


def test_access_log_line_is_structured(client, caplog):
    with caplog.at_level(logging.INFO, logger="ops.access"):
        client.get("/v1/stats", headers={**AUTH, "X-Request-ID": "req-42"})
    record = next(r for r in caplog.records if r.name == "ops.access")
    assert (record.method, record.path, record.status, record.api_key) == ("GET", "/v1/stats", 200, "tests")
    assert record.duration_ms >= 0

    token = request_id_var.set("req-42")
    try:
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert line["msg"] == "request"
    assert line["request_id"] == "req-42"
    assert line["path"] == "/v1/stats" and line["status"] == 200 and line["api_key"] == "tests"
    assert TEST_KEY not in json.dumps(line)


def test_json_formatter_includes_exception():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed %s", ("here",), sys.exc_info())
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "failed here" and "RuntimeError: boom" in line["exc"]
    assert "request_id" not in line


def test_search_text_is_literal_not_a_wildcard(client):
    for sid, body in (("q1", "50% deposit paid"), ("q2", "deposit paid in full")):
        payload = {"message_sid": sid, "from": "+12025550111", "body": body, "timestamp": 1793628000}
        assert client.post("/v1/ingest/sms", json=payload, headers=AUTH).status_code == 201

    def found(q):
        items = client.get("/v1/records", params={"q": q}, headers=AUTH).json()["items"]
        return sorted(item["external_id"] for item in items)

    assert found("DEPOSIT") == ["q1", "q2"]
    assert found("50%") == ["q1"]
    assert found("%") == ["q1"]
    assert found("_") == []


def test_out_of_range_timestamp_is_422_not_500(client):
    payload = {"message_sid": "big", "from": "+12025550111", "body": "x", "timestamp": 1e30}
    response = client.post("/v1/ingest/sms", json=payload, headers=AUTH)
    assert response.status_code == 422
    assert "out of range" in response.json()["detail"]["problems"][0]
