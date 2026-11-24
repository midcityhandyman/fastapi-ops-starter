"""Outbound notifications.

The default notifier is a mock that keeps messages in memory, so nothing
leaves the machine unless NOTIFY_WEBHOOK_URL is set. Either way the notify
job also writes an ``outbox`` row, which is what the API reports.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from app.config import Settings


class Notifier(Protocol):
    name: str

    def send(self, subject: str, body: str) -> None: ...


class MockNotifier:
    """Offline stand in. Remembers what it was asked to send."""

    name = "mock"

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, subject: str, body: str) -> None:
        self.sent.append({"subject": subject, "body": body})


class WebhookNotifier:
    """POST a small JSON document to a webhook (chat tool, pager, anything)."""

    name = "webhook"

    def __init__(self, url: str, token: str | None = None, client: httpx.Client | None = None):
        self.url = url
        self.token = token
        self.client = client or httpx.Client(timeout=5.0)

    def send(self, subject: str, body: str) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = self.client.post(self.url, json={"subject": subject, "body": body}, headers=headers)
        response.raise_for_status()


def build_notifier(settings: Settings) -> Notifier:
    if settings.notify_webhook_url:
        return WebhookNotifier(settings.notify_webhook_url, settings.notify_webhook_token)
    return MockNotifier()
