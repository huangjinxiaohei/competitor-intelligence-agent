"""Delivery adapters for publishing a digest without coupling to a host platform."""

from __future__ import annotations

import os
from typing import Protocol

import httpx

from .models import ChangeEvent, DeliveryReceipt, Digest


class DeliveryAdapter(Protocol):
    """Minimal host-neutral interface implemented by digest delivery adapters."""

    def publish(self, digest: Digest) -> DeliveryReceipt:
        """Publish ``digest`` and always return a receipt."""


def _change_line(change: ChangeEvent) -> str:
    return (
        f"**{change.candidate_id}** · `{change.field_path}`\n"
        f"{change.before!s} → {change.after!s} ({change.importance.value})"
    )


def render_payload(digest: Digest) -> dict:
    """Render a portable interactive-card payload for common bot webhooks.

    The payload follows the Feishu/Lark-style interactive-card envelope, which is
    also straightforward for a host adapter to translate.  A digest is bounded to
    five visible changes so a single notification remains scannable.
    """

    elements: list[dict] = [
        {"tag": "markdown", "text": {"content": digest.summary}},
    ]
    elements.extend(
        {"tag": "markdown", "text": {"content": _change_line(change)}}
        for change in digest.changes[:5]
    )
    elements.append(
        {
            "tag": "markdown",
            "text": {"content": f"Report: `{digest.report_path}`"},
        }
    )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": digest.title},
                "template": "blue",
            },
            "elements": elements,
        },
    }


class MockDeliveryAdapter:
    """In-memory adapter used by fixtures and tests."""

    def __init__(self) -> None:
        self.calls: list[Digest] = []

    def publish(self, digest: Digest) -> DeliveryReceipt:
        self.calls.append(digest)
        return DeliveryReceipt(adapter="mock", delivered=True, detail="recorded")


class WebhookDeliveryAdapter:
    """Publish structured digest cards to the configured collaboration webhook."""

    def __init__(self, webhook_url: str | None = None, timeout_seconds: float = 10.0) -> None:
        self._webhook_url = webhook_url
        self._timeout_seconds = timeout_seconds

    @property
    def webhook_url(self) -> str | None:
        """Read the URL lazily so environment injection remains testable."""
        return self._webhook_url or os.getenv("DELIVERY_WEBHOOK_URL")

    def publish(self, digest: Digest) -> DeliveryReceipt:
        url = self.webhook_url
        if not url:
            return DeliveryReceipt(
                adapter="webhook",
                delivered=False,
                detail="Webhook delivery is not configured.",
            )

        try:
            response = httpx.post(url, json=render_payload(digest), timeout=self._timeout_seconds)
        except httpx.HTTPError:
            return DeliveryReceipt(
                adapter="webhook",
                delivered=False,
                detail="Delivery failed while contacting the webhook.",
            )
        except Exception:
            return DeliveryReceipt(
                adapter="webhook",
                delivered=False,
                detail="Delivery failed unexpectedly.",
            )

        delivered = 200 <= response.status_code < 300
        return DeliveryReceipt(
            adapter="webhook",
            delivered=delivered,
            status_code=response.status_code,
            detail="Delivered." if delivered else f"Webhook returned HTTP {response.status_code}.",
        )
