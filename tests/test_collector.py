from pathlib import Path

from competitor_agent.collector import collect_candidate
from competitor_agent.config import ProjectConfig
from competitor_agent.models import Candidate, CandidateStatus, SourceType


def config() -> ProjectConfig:
    return ProjectConfig.model_validate({"project": {"id": "demo", "topic": "tools", "keywords": ["tools"]}, "collection": {"per_domain_delay_seconds": 0}})


def candidate(url: str = "fixture://acme") -> Candidate:
    return Candidate(id="acme", name="Acme", homepage=url, category="tools", score=.8, reasons=["test"], status=CandidateStatus.MONITORED)


def test_collect_candidate_reads_html_fixture_and_hashes_content(tmp_path: Path):
    (tmp_path / "acme.html").write_text("<html><title>Acme</title><body>Build agents quickly.</body></html>", encoding="utf-8")
    documents = collect_candidate(candidate(), config(), tmp_path)
    assert len(documents) == 1
    assert documents[0].source_type is SourceType.FIXTURE
    assert documents[0].title == "Acme"
    assert documents[0].text == "Acme Build agents quickly."
    assert len(documents[0].content_hash) == 64


def test_collect_candidate_uses_text_fixture_when_available(tmp_path: Path):
    (tmp_path / "acme.txt").write_text("Simple fixture text", encoding="utf-8")
    documents = collect_candidate(candidate(), config(), tmp_path)
    assert documents[0].text == "Simple fixture text"


def test_request_retries_three_times_after_initial_attempt(monkeypatch) -> None:
    from competitor_agent import collector
    attempts = 0
    sleeps: list[float] = []

    class FailingClient:
        def __init__(self, **_: object) -> None:
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_: object) -> None:
            return None
        def get(self, _: str):
            nonlocal attempts
            attempts += 1
            raise collector.httpx.ConnectError("offline")

    monkeypatch.setattr(collector.httpx, "Client", FailingClient)
    monkeypatch.setattr(collector.time, "sleep", sleeps.append)
    with __import__("pytest").raises(collector.httpx.ConnectError):
        collector._request("https://acme.test", config())
    assert attempts == 4
    assert sleeps == [1, 2, 4]


def test_domain_throttle_can_be_reset_and_waits_only_within_same_domain(monkeypatch) -> None:
    from competitor_agent import collector
    sleeps: list[float] = []
    ticks = iter([0.0, 0.0, 0.25, 1.0])
    collector.reset_throttle_state()
    monkeypatch.setattr(collector.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(collector.time, "sleep", sleeps.append)
    collector._throttle("https://acme.test/a", 1)
    collector._throttle("https://acme.test/b", 1)
    collector.reset_throttle_state()
    assert sleeps == [0.75]


def test_collect_candidate_handles_text_pdf_size_limit_and_browser_fallback(monkeypatch) -> None:
    from competitor_agent import collector
    from competitor_agent.models import SourceType
    request = collector.httpx.Request("GET", "https://acme.test/pricing.pdf")
    pdf_response = collector.httpx.Response(200, content=b"PDF fixture text", headers={"content-type": "application/pdf"}, request=request)
    monkeypatch.setattr(collector, "_request", lambda *_: pdf_response)
    assert collect_candidate(candidate("https://acme.test/pricing.pdf"), config())[0].source_type is SourceType.PDF

    oversized = collector.httpx.Response(200, content=b"x" * (1024 * 1024 + 1), request=request)
    monkeypatch.setattr(collector, "_request", lambda *_: oversized)
    limited_config = config().model_copy(update={"collection": config().collection.model_copy(update={"max_pdf_megabytes": 1})})
    with __import__("pytest").raises(ValueError, match="maximum collection size"):
        collect_candidate(candidate("https://acme.test/pricing.pdf"), limited_config)

    html_request = collector.httpx.Request("GET", "https://acme.test")
    shell = collector.httpx.Response(200, text="<html><script>render()</script></html>", request=html_request)
    monkeypatch.setattr(collector, "_request", lambda *_: shell)
    monkeypatch.setattr(
        collector,
        "_browser_text",
        lambda _: ("https://acme.test/rendered", "Rendered", "Rendered product body"),
    )
    rendered = collect_candidate(candidate("https://acme.test"), config())
    assert (rendered[0].title, rendered[0].text) == ("Rendered", "Rendered product body")
    assert rendered[0].url == "https://acme.test/rendered"


def test_browser_fallback_discards_rendered_content_after_off_domain_navigation(monkeypatch) -> None:
    from competitor_agent import collector

    html_request = collector.httpx.Request("GET", "https://acme.test")
    shell = collector.httpx.Response(
        200,
        text="<html><script>window.location = 'https://evil.test/landing'</script></html>",
        request=html_request,
    )
    monkeypatch.setattr(collector, "_request", lambda *_: shell)
    monkeypatch.setattr(
        collector,
        "_browser_text",
        lambda _: ("https://evil.test/landing", "External", "Off-domain rendered body"),
    )

    documents = collect_candidate(candidate("https://acme.test"), config())

    assert len(documents) == 1
    assert documents[0].url == "https://acme.test/"
    assert (documents[0].title, documents[0].text) == ("Acme", "")
