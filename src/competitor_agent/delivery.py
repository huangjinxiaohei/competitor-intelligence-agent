"""Delivery adapters for publishing a digest without coupling to a host platform."""

from __future__ import annotations

import os
from typing import Protocol

import httpx

from .models import ChangeEvent, DeliveryReceipt, Digest, DigestProduct


class DeliveryAdapter(Protocol):
    """Minimal host-neutral interface implemented by digest delivery adapters."""

    def publish(self, digest: Digest) -> DeliveryReceipt:
        """Publish ``digest`` and always return a receipt."""


def _change_line(change: ChangeEvent) -> str:
    return (
        f"**{change.candidate_id}** · `{change.field_path}`\n"
        f"{change.before!s} → {change.after!s} ({change.importance.value})"
    )


_UNKNOWN = "\u6682\u672a\u8bc6\u522b"
_MAX_NAME_LENGTH = 120
_MAX_HOME_URL_LENGTH = 300
_MAX_FEATURE_LENGTH = 120
_MAX_CONFIG_KEY_LENGTH = 80
_MAX_CONFIG_VALUE_LENGTH = 120
_MAX_EVIDENCE_URL_LENGTH = 300
_MAX_PRICE_PART_LENGTH = 100


def _truncate(value: object, limit: int) -> str:
    """Keep each visible field compact enough for webhook payload limits."""
    text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}\u2026"


def _price_text(product: DigestProduct) -> str:
    if not product.pricing:
        return _UNKNOWN

    values: list[str] = []
    for tier in product.pricing:
        amount = f"{tier.amount:g}" if tier.amount is not None else "\u8054\u7cfb\u9500\u552e"
        value = (
            f"{_truncate(tier.name, _MAX_PRICE_PART_LENGTH)}: "
            f"{_truncate(tier.currency or '', _MAX_PRICE_PART_LENGTH)} {amount}"
        ).replace("  ", " ").strip()
        if tier.period:
            value += f"/{_truncate(tier.period, _MAX_PRICE_PART_LENGTH)}"
        if tier.unit:
            value += f"/{_truncate(tier.unit, _MAX_PRICE_PART_LENGTH)}"
        values.append(value)
    return "; ".join(values)


def _product_block(product: DigestProduct) -> str:
    features = "; ".join(_truncate(feature, _MAX_FEATURE_LENGTH) for feature in product.features) or _UNKNOWN
    configurations = (
        "; ".join(
            f"{_truncate(key, _MAX_CONFIG_KEY_LENGTH)}={_truncate(value, _MAX_CONFIG_VALUE_LENGTH)}"
            for key, value in product.configurations.items()
        )
        or _UNKNOWN
    )
    evidence = (
        " ".join(
            f"[\u6765\u6e90{index}]({_truncate(url, _MAX_EVIDENCE_URL_LENGTH)})"
            for index, url in enumerate(product.evidence_urls, start=1)
        )
        or _UNKNOWN
    )
    return "\n".join(
        [
            f"### [{_truncate(product.name, _MAX_NAME_LENGTH)}]({_truncate(product.homepage, _MAX_HOME_URL_LENGTH)})",
            f"**\u6838\u5fc3\u529f\u80fd**\uFF1A{features}",
            f"**\u5957\u9910\u4ef7\u683c**\uFF1A{_price_text(product)}",
            f"**\u5173\u952e\u914d\u7f6e**\uFF1A{configurations}",
            f"**\u7f6e\u4fe1\u5ea6**\uFF1A{product.confidence:.0%}",
            f"**\u5b98\u65b9\u8bc1\u636e**\uFF1A{evidence}",
        ]
    )


def render_payload(digest: Digest) -> dict:
    """Render a portable interactive-card payload for common bot webhooks.

    The payload follows the Feishu/Lark-style interactive-card envelope, which is
    also straightforward for a host adapter to translate.  A digest is bounded to
    five visible changes so a single notification remains scannable.
    """

    elements: list[dict] = [
        {"tag": "markdown", "content": digest.summary},
    ]
    elements.extend(
        {"tag": "markdown", "content": _product_block(product)}
        for product in digest.products
    )
    if digest.changes:
        elements.append({"tag": "markdown", "content": "**\u91cd\u70b9\u53d8\u5316**"})
        elements.extend(
            {"tag": "markdown", "content": _change_line(change)}
            for change in digest.changes[:5]
        )
    elements.append(
        {
            "tag": "markdown",
            "content": f"\u672c\u5730\u5b8c\u6574\u62a5\u544a\uFF1A{digest.report_path}",
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
