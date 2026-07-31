"""Delivery adapters for publishing a digest without coupling to a host platform."""

from __future__ import annotations

import json
import os
from typing import Protocol

import httpx

from .models import ChangeEvent, DeliveryReceipt, Digest, DigestProduct


class DeliveryAdapter(Protocol):
    """Minimal host-neutral interface implemented by digest delivery adapters."""

    def publish(self, digest: Digest) -> DeliveryReceipt:
        """Publish ``digest`` and always return a receipt."""


_UNKNOWN = "\u6682\u672a\u8bc6\u522b"
_HOME_URL_TOO_LONG = "\u4e3b\u9875\u94fe\u63a5\u8fc7\u957f\uff0c\u89c1\u5b8c\u6574\u62a5\u544a"
_EVIDENCE_URL_TOO_LONG = "\u8bc1\u636e\u94fe\u63a5\u8fc7\u957f\uff0c\u89c1\u5b8c\u6574\u62a5\u544a"
_MAX_TITLE_LENGTH = 64
_MAX_SUMMARY_LENGTH = 200
_MAX_REPORT_PATH_LENGTH = 128
_MAX_NAME_LENGTH = 40
_MAX_URL_LENGTH = 512
_MAX_PAYLOAD_BYTES = 19_500
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


def _product_block(product: DigestProduct, base_record_url: str | None = None) -> str:
    features = "; ".join(_truncate(feature, _MAX_FEATURE_LENGTH) for feature in product.features) or _UNKNOWN
    configurations = "; ".join(f"{_truncate(key, _MAX_CONFIG_KEY_LENGTH)}={_truncate(value, _MAX_CONFIG_VALUE_LENGTH)}" for key, value in product.configurations.items()) or _UNKNOWN
    evidence = " ".join(_display_url(url, f"\u6765\u6e90{index}", _EVIDENCE_URL_TOO_LONG) for index, url in enumerate(product.evidence_urls, start=1)) or _UNKNOWN
    name = _truncate(product.name, _MAX_NAME_LENGTH)
    heading = _display_url(product.homepage, name, _HOME_URL_TOO_LONG)
    heading = f"### {heading}" if heading != _HOME_URL_TOO_LONG else f"### {name}\n{heading}"
    confidence = f"{product.confidence:.0%}" if product.confidence > 0 else _UNKNOWN
    lines = [heading, f"**\u6838\u5fc3\u529f\u80fd**\uff1a{features}", f"**\u5957\u9910\u4ef7\u683c**\uff1a{_price_text(product)}", f"**\u5173\u952e\u914d\u7f6e**\uff1a{configurations}", f"**\u7f6e\u4fe1\u5ea6**\uff1a{confidence}", f"**\u5b98\u65b9\u8bc1\u636e**\uff1a{evidence}"]
    if base_record_url:
        lines.append(f"**Base\u8bb0\u5f55**\uff1a{_display_url(base_record_url, 'Base\u8bb0\u5f55', _EVIDENCE_URL_TOO_LONG)}")
    return "\n".join(lines)


def _change_line(change: ChangeEvent) -> str:
    return (
        f"**{_truncate(change.candidate_id, _MAX_CHANGE_ID_LENGTH)}** \u00b7 "
        f"`{_truncate(change.field_path, _MAX_CHANGE_FIELD_LENGTH)}`\n"
        f"{_truncate(change.before, _MAX_CHANGE_VALUE_LENGTH)} \u2192 "
        f"{_truncate(change.after, _MAX_CHANGE_VALUE_LENGTH)} ({change.importance.value})"
    )


def _serialized_size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _fit_link_budget(payload: dict, digest: Digest) -> None:
    """Downgrade complete links only when the whole card exceeds its byte budget."""
    if _serialized_size(payload) < _MAX_PAYLOAD_BYTES:
        return

    products = digest.products[:5]
    replacements: list[tuple[int, str, str]] = []
    for evidence_index in (1, 0):
        for product_index, product in enumerate(products):
            if evidence_index >= len(product.evidence_urls):
                continue
            url = product.evidence_urls[evidence_index]
            if len(url) <= _MAX_URL_LENGTH:
                label = f"\u6765\u6e90{evidence_index + 1}"
                replacements.append(
                    (
                        product_index + 1,
                        f"[{label}]({url})",
                        _EVIDENCE_URL_TOO_LONG,
                    )
                )

    for product_index, product in enumerate(products):
        if len(product.homepage) <= _MAX_URL_LENGTH:
            name = _truncate(product.name, _MAX_NAME_LENGTH)
            replacements.append(
                (
                    product_index + 1,
                    f"### [{name}]({product.homepage})",
                    f"### {name}\n{_HOME_URL_TOO_LONG}",
                )
            )

    for element_index, link, fallback in replacements:
        element = payload["card"]["elements"][element_index]
        content = element["content"]
        if link not in content:
            continue
        element["content"] = content.replace(link, fallback, 1)
        if _serialized_size(payload) < _MAX_PAYLOAD_BYTES:
            return


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
        {"tag": "markdown", "content": _product_block(product, digest.base_links.get(f"record:{product.candidate_id}"))}
        for product in digest.products[:5]
    )
    if digest.base_links:
        preferred = ("\u7ade\u54c1\u603b\u89c8", "\u672c\u5468\u53d8\u5316", "\u4ef7\u683c\u5bf9\u6bd4")
        links = " \u00b7 ".join(
            _display_url(digest.base_links[label], label, _EVIDENCE_URL_TOO_LONG)
            for label in preferred if digest.base_links.get(label)
        )
        if links:
            elements.append({"tag": "markdown", "content": f"**\u98de\u4e66 Base**\uff1a{links}"})
    elif digest.projection is not None and not digest.projection.synced:
        elements.append({"tag": "markdown", "content": "**Base\u540c\u6b65\u5f85\u91cd\u8bd5**\uff1a\u672c\u6b21\u7ed3\u8bba\u4ecd\u53ef\u901a\u8fc7\u5b98\u65b9\u8bc1\u636e\u6838\u9a8c\u3002"})
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
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": _truncate(digest.title, _MAX_TITLE_LENGTH)},
                "template": "blue",
            },
            "elements": elements,
        },
    }
    _fit_link_budget(payload, digest)
    return payload


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
