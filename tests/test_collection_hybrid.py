from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from competitor_agent.analyzer import HybridOpenAIAnalyzer, analyze_documents
from competitor_agent.collector import collect_candidate
from competitor_agent.config import ProjectConfig
from competitor_agent.models import Candidate, CandidateStatus, SourceDocument, SourceType


def _config(max_pages: int = 6) -> ProjectConfig:
    return ProjectConfig.model_validate({
        "project": {"id": "demo", "topic": "project tools", "keywords": ["project"]},
        "collection": {"per_domain_delay_seconds": 0, "max_pages_per_candidate": max_pages},
    })


def _candidate(**updates: object) -> Candidate:
    payload: dict[str, object] = {
        "id": "candidate-demo",
        "name": "CloudBase",
        "homepage": "https://app.example.test",
        "category": "project tools",
        "score": .9,
        "reasons": ["test"],
        "status": CandidateStatus.MONITORED,
        "official_entry_urls": [],
    }
    payload.update(updates)
    return Candidate.model_validate(payload)


def _response(url: str, html: str) -> httpx.Response:
    return httpx.Response(200, text=html, request=httpx.Request("GET", url))


def test_collection_fetches_official_entries_then_same_domain_keyword_links_in_stable_order(monkeypatch) -> None:
    from competitor_agent import collector

    pages = {
        "https://app.example.test/pricing": "<title>Pricing</title><a href='/features'>Features</a><a href='https://evil.test/pricing'>Bad</a>",
        "https://app.example.test/docs": "<title>Docs</title><a href='/product'>Product</a><a href='/pricing?utm_source=x'>Pricing</a>",
        "https://app.example.test/": "<title>Home</title><a href='/security'>Security</a>",
        "https://app.example.test/features": "<title>Features</title>Feature text",
    }
    calls: list[str] = []

    def request(url: str, _: ProjectConfig) -> httpx.Response:
        calls.append(url)
        return _response(url, pages[url])

    monkeypatch.setattr(collector, "_request", request)
    documents = collect_candidate(
        _candidate(official_entry_urls=["https://app.example.test/pricing", "https://app.example.test/docs"]),
        _config(4),
    )
    assert calls == [
        "https://app.example.test/pricing",
        "https://app.example.test/docs",
        "https://app.example.test/",
        "https://app.example.test/features",
    ]
    assert [item.url for item in documents] == calls


def test_collection_rejects_off_domain_and_caps_fixture_pages(tmp_path: Path) -> None:
    (tmp_path / "cloudbase.html").write_text(
        "<title>CloudBase</title><a href='fixture://cloudbase/pricing'>Pricing</a><a href='fixture://other/pricing'>Other</a>",
        encoding="utf-8",
    )
    (tmp_path / "cloudbase-pricing.html").write_text("<title>Pricing</title>Starter $12 per month", encoding="utf-8")
    documents = collect_candidate(
        _candidate(homepage="fixture://cloudbase", official_entry_urls=[]), _config(1), tmp_path
    )
    assert len(documents) == 1
    assert documents[0].url == "fixture://cloudbase"

    documents = collect_candidate(
        _candidate(homepage="fixture://cloudbase", official_entry_urls=[]), _config(3), tmp_path
    )
    assert [item.url for item in documents] == ["fixture://cloudbase", "fixture://cloudbase/pricing"]


@pytest.mark.parametrize("fixture_name, expected", [
    ("cloudbase.html", ("CloudBase", "Starter", 12.0, "billed annually")),
    ("streamdesk.html", ("StreamDesk", "Team", 24.0, "annual commitment required")),
    ("safevault.html", ("SafeVault", "Business", 49.0, "starting at")),
])
def test_anonymous_saas_fixtures_extract_clean_pricing_and_field_evidence(fixture_name: str, expected: tuple[str, str, float, str]) -> None:
    fixture = Path(__file__).parent / "fixtures" / "anonymous_saas" / fixture_name
    doc = SourceDocument(
        candidate_id="candidate-demo", url=f"fixture://{fixture_name}", title="Fixture",
        fetched_at=datetime.now(UTC), content_hash="a" * 64, source_type=SourceType.FIXTURE,
        text=fixture.read_text(encoding="utf-8"),
    )
    snapshot = analyze_documents(_candidate(name=expected[0]), [doc])
    assert any(item.name == expected[1] and item.amount == expected[2] and expected[3] in item.qualifiers for item in snapshot.pricing)
    assert snapshot.features
    assert snapshot.field_evidence
    assert all(snapshot.field_evidence.values())


def test_rule_extraction_never_fabricates_price_and_records_contact_sales() -> None:
    doc = SourceDocument(
        candidate_id="candidate-demo", url="https://app.example.test/pricing", title="Pricing",
        fetched_at=datetime.now(UTC), content_hash="b" * 64, source_type=SourceType.HTML,
        text="Enterprise plan — Contact sales. Features include audit logs and SSO.",
    )
    snapshot = analyze_documents(_candidate(), [doc])
    enterprise = next(item for item in snapshot.pricing if item.name == "Enterprise")
    assert enterprise.amount is None
    assert "contact sales" in enterprise.qualifiers
    assert "pricing.enterprise.amount" not in snapshot.field_evidence


def _model_document() -> SourceDocument:
    return SourceDocument(
        candidate_id="candidate-demo", url="https://app.example.test/pricing", title="Pricing",
        fetched_at=datetime.now(UTC), content_hash="c" * 64, source_type=SourceType.HTML,
        text="Pro plan $29 per month per editor. Configure deployment: cloud. Includes audit logs. Annual billing available. Priority support is included. Advanced reporting is available. Service is offered internationally.",
    )


def _configure_model(monkeypatch) -> str:
    endpoint = "https://model.example.test/v1/chat/completions"
    monkeypatch.setenv("MODEL_API_URL", endpoint)
    monkeypatch.setenv("MODEL_API_KEY", "secret")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    return endpoint


def _model_snapshot(*, candidate_id: str = "candidate-demo", excerpt: str | None = None, amount: float = 999) -> dict[str, object]:
    document = _model_document()
    def evidence(value: str) -> dict[str, str]:
        return {"source_url": document.url, "excerpt": value, "observed_at": document.fetched_at.isoformat()}
    price = evidence("Pro plan $29 per month per editor.")
    feature = evidence("Includes audit logs.")
    advanced = evidence("Advanced reporting is available.")
    deployment = evidence("Configure deployment: cloud.")
    support = evidence("Priority support is included.")
    availability = evidence("Service is offered internationally.")
    qualifier = evidence("Annual billing available.")
    if excerpt is not None:
        availability = evidence(excerpt)
    return {
        "candidate_id": candidate_id,
        "observed_at": document.fetched_at.isoformat(),
        "summary": "\u6a21\u578b\u4e2d\u6587\u6458\u8981",
        "features": ["audit logs", "advanced reporting"],
        "specifications": {},
        "configurations": {"deployment": "cloud", "support": "priority"},
        "pricing": [{"name": "Pro", "amount": amount, "currency": "USD", "period": "month", "unit": "editor", "qualifiers": ["annual billing available"]}],
        "availability": "\u5168\u7403\u53ef\u7528",
        "confidence": .99,
        "evidence": [price, feature, advanced, deployment, support, availability, qualifier],
        "field_evidence": {
            "summary": [price], "features.0": [feature], "features.1": [advanced],
            "configurations.deployment": [deployment], "configurations.support": [support],
            "availability": [availability], "pricing.pro.qualifiers": [qualifier],
        },
    }


def test_hybrid_openai_merges_only_semantics_and_preserves_rule_numeric_price(monkeypatch) -> None:
    endpoint = _configure_model(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(endpoint).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": __import__("json").dumps(_model_snapshot())}}]}))
        snapshot = HybridOpenAIAnalyzer().analyze(_candidate(), [_model_document()])
    pro = next(item for item in snapshot.pricing if item.name == "Pro")
    assert pro.amount == 29.0
    assert pro.qualifiers == ["annual billing available"]
    assert snapshot.availability == "全球可用"
    assert snapshot.summary == "模型中文摘要"
    assert snapshot.confidence < .99


@pytest.mark.parametrize("payload", [
    {"choices": [{"message": {"content": "{bad"}}]},
    {"choices": [{"message": {"content": __import__("json").dumps(_model_snapshot(candidate_id="wrong"))}}]},
    {"choices": [{"message": {"content": __import__("json").dumps(_model_snapshot(excerpt="not in official source"))}}]},
])
def test_hybrid_retries_invalid_or_unmatched_model_output_then_falls_back(monkeypatch, payload: dict[str, object]) -> None:
    endpoint = _configure_model(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(endpoint).mock(return_value=httpx.Response(200, json=payload))
        analyzer = HybridOpenAIAnalyzer()
        snapshot = analyzer.analyze(_candidate(), [_model_document()])
    assert snapshot.summary.startswith("CloudBase:")
    assert len(route.calls) == 2
    assert analyzer.last_diagnostic == "model_degraded: invalid model response"


@pytest.mark.parametrize("error", [httpx.TimeoutException("timeout"), httpx.Response(429)])
def test_hybrid_falls_back_on_transport_or_rate_limit(monkeypatch, error: object) -> None:
    endpoint = _configure_model(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        if isinstance(error, httpx.Response):
            mock.post(endpoint).mock(return_value=error)
        else:
            mock.post(endpoint).mock(side_effect=error)
        analyzer = HybridOpenAIAnalyzer()
        snapshot = analyzer.analyze(_candidate(), [_model_document()])
    assert snapshot.pricing[0].amount == 29
    assert analyzer.last_diagnostic == "model_degraded: request failed"


def test_hybrid_does_not_adopt_a_model_only_numeric_price(monkeypatch) -> None:
    endpoint = _configure_model(monkeypatch)
    document = _model_document().model_copy(update={"text": "Includes audit logs. Configure deployment: cloud."})
    with respx.mock(assert_all_called=True) as mock:
        mock.post(endpoint).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": __import__("json").dumps(_model_snapshot())}}]}))
        snapshot = HybridOpenAIAnalyzer().analyze(_candidate(), [document])
    assert snapshot.pricing == []


def test_collection_skips_official_url_when_redirected_off_domain(monkeypatch) -> None:
    from competitor_agent import collector

    redirected = httpx.Response(200, text="external page", request=httpx.Request("GET", "https://evil.test/landing"))
    monkeypatch.setattr(collector, "_request", lambda *_: redirected)
    assert collect_candidate(_candidate(), _config()) == []


def test_hybrid_omits_model_field_without_direct_field_evidence(monkeypatch) -> None:
    endpoint = _configure_model(monkeypatch)
    document = _model_document().model_copy(update={
        "text": "Pro plan $29 per month per editor. Configure deployment: cloud. Includes audit logs. Annual billing available. Priority support is included. Advanced reporting is available."
    })
    payload = _model_snapshot()
    payload["field_evidence"].pop("availability")
    payload["evidence"] = [item for item in payload["evidence"] if item["excerpt"] != "Service is offered internationally."]
    with respx.mock(assert_all_called=True) as mock:
        mock.post(endpoint).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": __import__("json").dumps(payload)}}]}))
        snapshot = HybridOpenAIAnalyzer().analyze(_candidate(), [document])
    assert snapshot.availability is None
