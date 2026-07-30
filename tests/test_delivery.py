from datetime import UTC, datetime

import httpx

from competitor_agent.delivery import MockDeliveryAdapter, WebhookDeliveryAdapter, render_payload
from competitor_agent.models import ChangeEvent, ChangeImportance, Digest


def make_digest(change_count: int = 6) -> Digest:
    changes = [
        ChangeEvent(
            candidate_id=f"nova-{index}",
            field_path="pricing.pro.amount",
            before=29 + index,
            after=39 + index,
            importance=ChangeImportance.HIGH,
        )
        for index in range(change_count)
    ]
    return Digest(
        run_id="run-001",
        kind="daily",
        title="Competitor update",
        summary="Pricing and feature changes detected.",
        changes=changes,
        report_path="reports/run-001.md",
    )


def test_render_payload_is_collaboration_card_and_limits_changes() -> None:
    payload = render_payload(make_digest())

    assert payload["msg_type"] == "interactive"
    assert payload["card"]["header"]["title"]["content"] == "Competitor update"
    assert len(payload["card"]["elements"]) == 7  # summary + five changes + report link
    assert payload["card"]["elements"][0] == {
        "tag": "markdown",
        "content": "Pricing and feature changes detected.",
    }
    assert "nova-0" in payload["card"]["elements"][1]["content"]
    assert "nova-5" not in str(payload)


def test_mock_adapter_records_the_digest() -> None:
    adapter = MockDeliveryAdapter()
    digest = make_digest(1)

    receipt = adapter.publish(digest)

    assert receipt.delivered is True
    assert receipt.adapter == "mock"
    assert adapter.calls == [digest]


def test_webhook_adapter_posts_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, json: dict, timeout: float) -> httpx.Response:
        captured.update(url=url, json=json, timeout=timeout)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("DELIVERY_WEBHOOK_URL", "https://hooks.example.test/bot/secret")
    monkeypatch.setattr("competitor_agent.delivery.httpx.post", fake_post)

    receipt = WebhookDeliveryAdapter().publish(make_digest(1))

    assert receipt.delivered is True
    assert receipt.status_code == 200
    assert captured["url"] == "https://hooks.example.test/bot/secret"
    assert captured["json"]["msg_type"] == "interactive"


def test_webhook_adapter_returns_failure_receipt_without_url(monkeypatch) -> None:
    monkeypatch.delenv("DELIVERY_WEBHOOK_URL", raising=False)

    receipt = WebhookDeliveryAdapter().publish(make_digest(1))

    assert receipt.delivered is False
    assert receipt.status_code is None
    assert "not configured" in receipt.detail.lower()


def test_webhook_adapter_returns_failure_receipt_on_transport_error(monkeypatch) -> None:
    monkeypatch.setenv("DELIVERY_WEBHOOK_URL", "https://hooks.example.test/bot/secret")

    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr("competitor_agent.delivery.httpx.post", fake_post)

    receipt = WebhookDeliveryAdapter().publish(make_digest(1))

    assert receipt.delivered is False
    assert receipt.adapter == "webhook"
    assert "delivery failed" in receipt.detail.lower()


def test_webhook_failure_receipt_never_exposes_the_webhook_secret(monkeypatch) -> None:
    secret_url = "https://hooks.example.test/bot/very-secret-token"
    monkeypatch.setenv("DELIVERY_WEBHOOK_URL", secret_url)

    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr("competitor_agent.delivery.httpx.post", fake_post)

    receipt = WebhookDeliveryAdapter().publish(make_digest(1))

    assert secret_url not in receipt.detail
    assert "very-secret-token" not in receipt.detail
