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
_HOME_URL_TOO_LONG = "\u4e3b\u9875\u94fe\u63a5\u8fc7\u957f\uff0c\u89c1\u5b8c\u6574\u62a5\u544a"
_EVIDENCE_URL_TOO_LONG = "\u8bc1\u636e\u94fe\u63a5\u8fc7\u957f\uff0c\u89c1\u5b8c\u6574\u62a5\u544a"
_MAX_TITLE_LENGTH = 64
_MAX_SUMMARY_LENGTH = 200
_MAX_REPORT_PATH_LENGTH = 128
_MAX_NAME_LENGTH = 40
_MAX_URL_LENGTH = 64
_MAX_FEATURE_LENGTH = 32
_MAX_CONFIG_KEY_LENGTH = 16
_MAX_CONFIG_VALUE_LENGTH = 32
_MAX_PRICE_NAME_LENGTH = 24
_MAX_CURRENCY_LENGTH = 12
_MAX_PERIOD_LENGTH = 12
_MAX_UNIT_LENGTH = 12
_MAX_CHANGE_ID_LENGTH = 36
_MAX_CHANGE_FIELD_LENGTH = 36
_MAX_CHANGE_VALUE_LENGTH = 48


def _truncate(value: object, limit: int) -> str:
    """Keep descriptive text compact without modifying links."""
    text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}\u2026"


def _display_url(url: str, label: str, too_long_copy: str) -> str:
    """Return a complete Markdown link or a non-link fallback; never a partial URL."""
    if len(url) > _MAX_URL_LENGTH:
        return too_long_copy
    return f"[{label}]({url})"


def _price_text(product: DigestProduct) -> str:
    if not product.pricing:
        return _UNKNOWN

    values: list[str] = []
    for tier in product.pricing:
        amount = f"{tier.amount:g}" if tier.amount is not None else _UNKNOWN
        value = f"{_truncate(tier.name, _MAX_PRICE_NAME_LENGTH)}: "
        if tier.currency:
            value += f"{_truncate(tier.currency, _MAX_CURRENCY_LENGTH)} "
        value += amount
        if tier.period:
            value += f"/{_truncate(tier.period, _MAX_PERIOD_LENGTH)}"
        if tier.unit:
            value += f"/{_truncate(tier.unit, _MAX_UNIT_LENGTH)}"
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
            _display_url(url, f"\u6765\u6e90{index}", _EVIDENCE_URL_TOO_LONG)
            for index, url in enumerate(product.evidence_urls, start=1)
        )
        or _UNKNOWN
    )
    name = _truncate(product.name, _MAX_NAME_LENGTH)
    heading = _display_url(product.homepage, name, _HOME_URL_TOO_LONG)
    if heading == _HOME_URL_TOO_LONG:
        heading = f"### {name}\n{heading}"
    else:
        heading = f"### {heading}"
    confidence = f"{product.confidence:.0%}" if product.confidence > 0 else _UNKNOWN
    return "\n".join(
        [
            heading,
            f"**\u6838\u5fc3\u529f\u80fd**\uFF1A{features}",
            f"**\u5957\u9910\u4ef7\u683c**\uFF1A{_price_text(product)}",
            f"**\u5173\u952e\u914d\u7f6e**\uFF1A{configurations}",
            f"**\u7f6e\u4fe1\u5ea6**\uFF1A{confidence}",
            f"**\u5b98\u65b9\u8bc1\u636e**\uFF1A{evidence}",
        ]
    )


def _change_line(change: ChangeEvent) -> str:
    return (
        f"**{_truncate(change.candidate_id, _MAX_CHANGE_ID_LENGTH)}** \u00b7 "
        f"`{_truncate(change.field_path, _MAX_CHANGE_FIELD_LENGTH)}`\n"
        f"{_truncate(change.before, _MAX_CHANGE_VALUE_LENGTH)} \u2192 "
        f"{_truncate(change.after, _MAX_CHANGE_VALUE_LENGTH)} ({change.importance.value})"
    )


def render_payload(digest: Digest) -> dict:
    """Render a portable interactive-card payload for common bot webhooks.

    The payload follows the Feishu/Lark-style interactive-card envelope, which is
    also straightforward for a host adapter to translate.  A digest is bounded to
    five visible changes so a single notification remains scannable.
    """

    elements: list[dict] = [
        {"tag": "markdown", "content": _truncate(digest.summary, _MAX_SUMMARY_LENGTH)},
    ]
    elements.extend(
        {"tag": "markdown", "content": _product_block(product)}
        for product in digest.products[:5]
    )
    confirmed_changes = [change for change in digest.changes if change.confirmed]
    if confirmed_changes:
        elements.append({"tag": "markdown", "content": "**\u91cd\u70b9\u53d8\u5316**"})
        elements.extend(
            {"tag": "markdown", "content": _change_line(change)}
            for change in confirmed_changes[:5]
        )
    elements.append(
        {
            "tag": "markdown",
            "content": f"\u672c\u5730\u5b8c\u6574\u62a5\u544a\uFF1A{_truncate(digest.report_path, _MAX_REPORT_PATH_LENGTH)}",
        }
    )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": _truncate(digest.title, _MAX_TITLE_LENGTH)},
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
