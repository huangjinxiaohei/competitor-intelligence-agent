from datetime import UTC, datetime

from competitor_agent.analyzer import Analyzer, analyze_documents
from competitor_agent.models import Candidate, CandidateStatus, SourceDocument, SourceType


def candidate() -> Candidate:
    return Candidate(id="acme", name="Acme", homepage="https://acme.test", category="agent", score=.8, reasons=["test"], status=CandidateStatus.MONITORED)


def document() -> SourceDocument:
    return SourceDocument(candidate_id="acme", url="https://acme.test/pricing", title="Acme Pricing", fetched_at=datetime.now(UTC), content_hash="a" * 64, source_type=SourceType.HTML, text="Acme lets teams build AI agents. Features include workflow automation and API access. Pro plan $49 per month per user. Supports 10,000 requests per month. Configure model: GPT-4.")


def test_analyze_documents_extracts_facts_with_evidence():
    snapshot = analyze_documents(candidate(), [document()])
    assert snapshot.candidate_id == "acme"
    assert "workflow automation" in snapshot.features
    assert snapshot.pricing[0].amount == 49
    assert snapshot.specifications["requests_per_month"] == 10000
    assert snapshot.configurations["model"] == "GPT-4"
    assert snapshot.evidence


def test_analyzer_protocol_is_runtime_checkable():
    class StubAnalyzer:
        def analyze(self, candidate, documents):
            return analyze_documents(candidate, documents)
    assert isinstance(StubAnalyzer(), Analyzer)


def test_analyze_documents_returns_empty_snapshot_when_no_facts() -> None:
    snapshot = analyze_documents(candidate(), [])
    assert snapshot.summary == ""
    assert snapshot.confidence == 0
    assert snapshot.evidence == []


def test_analyze_documents_deduplicates_evidence_and_extracts_plan_and_deployment() -> None:
    source = document().model_copy(update={
        "text": "Pro plan: $29 per month per editor. Configuration deployment: cloud.",
    })
    snapshot = analyze_documents(candidate(), [source, source])
    assert snapshot.pricing == [snapshot.pricing[0].model_copy(update={"name": "Pro", "amount": 29.0, "currency": "USD", "period": "month", "unit": "editor"})]
    assert snapshot.configurations["deployment"] == "cloud"
    assert len(snapshot.evidence) == 1
    assert len(set((item.source_url, item.excerpt, item.observed_at) for item in snapshot.evidence)) == 1




def _configure_model(monkeypatch) -> str:
    endpoint = "https://model.example.test/analyze?access_token=hidden"
    monkeypatch.setenv("MODEL_API_URL", endpoint)
    monkeypatch.setenv("MODEL_API_KEY", "super-secret-key")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    return endpoint


def _model_snapshot_payload() -> dict:
    return analyze_documents(candidate(), [document()]).model_dump(mode="json")


def test_http_json_analyzer_posts_official_documents_and_schema(monkeypatch) -> None:
    import json
    import httpx
    import respx
    from competitor_agent.analyzer import HttpJsonAnalyzer

    endpoint = _configure_model(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(endpoint).mock(return_value=httpx.Response(200, json=_model_snapshot_payload()))
        result = HttpJsonAnalyzer().analyze(candidate(), [document()])
    body = json.loads(route.calls[0].request.content)
    assert result.candidate_id == candidate().id
    assert body["model"] == "test-model"
    assert body["candidate"]["homepage"] == "https://acme.test"
    assert body["documents"][0]["text"] == document().text
    assert body["response_schema"]["title"] == "ProductSnapshot"
    assert route.calls[0].request.headers["authorization"] == "Bearer super-secret-key"


def test_http_json_analyzer_retries_once_with_validation_errors(monkeypatch) -> None:
    import httpx
    import respx
    from competitor_agent.analyzer import HttpJsonAnalyzer

    endpoint = _configure_model(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(endpoint).mock(side_effect=[
            httpx.Response(200, text="{bad json"),
            httpx.Response(200, json=_model_snapshot_payload()),
        ])
        result = HttpJsonAnalyzer().analyze(candidate(), [document()])
    assert result.candidate_id == "acme"
    assert len(route.calls) == 2
    assert b"validation_errors" in route.calls[1].request.content


def test_http_json_analyzer_rejects_two_invalid_schema_responses(monkeypatch) -> None:
    import httpx
    import pytest
    import respx
    from competitor_agent.analyzer import HttpJsonAnalyzer

    endpoint = _configure_model(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(endpoint).mock(side_effect=[
            httpx.Response(200, json={"candidate_id": "acme"}),
            httpx.Response(200, json={"candidate_id": "other"}),
        ])
        with pytest.raises(RuntimeError, match="invalid ProductSnapshot"):
            HttpJsonAnalyzer().analyze(candidate(), [document()])
    assert len(route.calls) == 2


def test_http_json_analyzer_requires_environment_configuration(monkeypatch) -> None:
    import pytest
    from competitor_agent.analyzer import HttpJsonAnalyzer

    for name in ("MODEL_API_URL", "MODEL_API_KEY", "MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="MODEL_API_URL"):
        HttpJsonAnalyzer()


def test_analyze_documents_stops_configuration_value_at_sentence_and_next_label() -> None:
    source = document().model_copy(update={
        "text": "Configure model: Standard. Pricing details are temporarily omitted. Configuration deployment: cloud-hosted Pricing: $29 per month.",
    })
    snapshot = analyze_documents(candidate(), [source])
    assert snapshot.configurations == {"model": "Standard", "deployment": "cloud-hosted"}


def test_analyze_documents_stops_configuration_values_at_semicolons_and_newlines() -> None:
    source = document().model_copy(update={
        "text": "Configure region: North America; Configuration deployment: cloud-hosted\nConfigure model: GPT 4 Turbo",
    })
    snapshot = analyze_documents(candidate(), [source])
    assert snapshot.configurations == {
        "region": "North America",
        "deployment": "cloud-hosted",
        "model": "GPT 4 Turbo",
    }


def test_analyze_documents_does_not_include_page_title_in_pricing_tier() -> None:
    source = document().model_copy(update={"text": "NovaBoard Pro plan $29 per month per editor."})
    snapshot = analyze_documents(candidate(), [source])
    assert snapshot.pricing[0].name == "Pro"
