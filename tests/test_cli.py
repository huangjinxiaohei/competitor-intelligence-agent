from typer.testing import CliRunner

from competitor_agent.cli import app


runner = CliRunner()


def test_doctor_succeeds_in_fixture_mode_without_webhook(monkeypatch) -> None:
    monkeypatch.delenv("DELIVERY_WEBHOOK_URL", raising=False)

    result = runner.invoke(app, ["doctor", "--fixture"])

    assert result.exit_code == 0
    assert "fixture mode" in result.stdout.lower()
    assert "webhook" in result.stdout.lower()


def test_discover_forwards_fixture_and_dry_run_to_pipeline(monkeypatch) -> None:
    received: dict[str, object] = {}

    def fake_discover_only(**kwargs):
        received.update(kwargs)
        return {"candidates": 2}

    monkeypatch.setattr("competitor_agent.cli._pipeline_function", lambda name: fake_discover_only)

    result = runner.invoke(app, ["discover", "--fixture", "--dry-run"])

    assert result.exit_code == 0
    assert received["fixture"] is True
    assert received["dry_run"] is True
    assert '"candidates": 2' in result.stdout


def test_run_forwards_delivery_mode(monkeypatch) -> None:
    received: dict[str, object] = {}

    def fake_run_pipeline(**kwargs):
        received.update(kwargs)
        return {"run_id": "run-001"}

    monkeypatch.setattr("competitor_agent.cli._pipeline_function", lambda name: fake_run_pipeline)

    result = runner.invoke(app, ["run", "--fixture", "--dry-run"])

    assert result.exit_code == 0
    assert received["fixture"] is True
    assert received["dry_run"] is True
    assert '"run_id": "run-001"' in result.stdout


def test_report_accepts_run_id(monkeypatch) -> None:
    received: dict[str, object] = {}

    def fake_report_run(**kwargs):
        received.update(kwargs)
        return "reports/run-001.md"

    monkeypatch.setattr("competitor_agent.cli._pipeline_function", lambda name: fake_report_run)

    result = runner.invoke(app, ["report", "run-001", "--fixture"])

    assert result.exit_code == 0
    assert received["run_id"] == "run-001"
    assert received["fixture"] is True


def test_push_test_uses_mock_delivery(monkeypatch) -> None:
    received: dict[str, object] = {}

    def fake_run_pipeline(**kwargs):
        received.update(kwargs)
        return {"delivery": {"delivered": True}}

    monkeypatch.setattr("competitor_agent.cli._pipeline_function", lambda name: fake_run_pipeline)

    result = runner.invoke(app, ["push-test", "--fixture"])

    assert result.exit_code == 0
    assert received["delivery_adapter"] == "mock"
    assert received["force_publish"] is True
    assert received["fixture"] is True

# Fixture acceptance is kept here because these fixtures are the CLI's offline contract.
def test_three_round_fixtures_have_expected_structured_changes_and_stable_round_three() -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from bs4 import BeautifulSoup

    from competitor_agent.analyzer import analyze_documents
    from competitor_agent.models import Candidate, CandidateStatus, SourceDocument, SourceType

    root = Path("fixtures")

    def snapshot(round_name: str, product: str):
        html = (root / round_name / f"{product}.html").read_text(encoding="utf-8")
        candidate = Candidate(
            id=product,
            name=product.title(),
            homepage=f"fixture://{product}",
            category="workspace",
            score=0.9,
            reasons=["fixture"],
            status=CandidateStatus.MONITORED,
        )
        document = SourceDocument(
            candidate_id=product,
            url=f"fixture://{round_name}/{product}",
            title=product.title(),
            fetched_at=datetime.now(UTC),
            content_hash="f" * 64,
            source_type=SourceType.FIXTURE,
            text=BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
        )
        return analyze_documents(candidate, [document])

    baseline_nova = snapshot("round_01_baseline", "novaboard")
    baseline_orbit = snapshot("round_01_baseline", "orbitnote")
    changed_nova = snapshot("round_02_changed", "novaboard")
    changed_orbit = snapshot("round_02_changed", "orbitnote")
    stable_nova = snapshot("round_03_changed", "novaboard")
    stable_orbit = snapshot("round_03_changed", "orbitnote")

    assert baseline_nova.pricing and baseline_nova.configurations
    assert baseline_orbit.pricing and baseline_orbit.configurations
    assert baseline_nova.pricing != changed_nova.pricing
    assert baseline_orbit.configurations != changed_orbit.configurations
    assert baseline_nova.features != changed_nova.features
    assert (stable_nova.features, stable_nova.specifications, stable_nova.configurations, stable_nova.pricing) == (
        changed_nova.features,
        changed_nova.specifications,
        changed_nova.configurations,
        changed_nova.pricing,
    )
    assert (stable_orbit.features, stable_orbit.specifications, stable_orbit.configurations, stable_orbit.pricing) == (
        changed_orbit.features,
        changed_orbit.specifications,
        changed_orbit.configurations,
        changed_orbit.pricing,
    )


def test_fixture_html_files_are_utf8_without_a_byte_order_mark() -> None:
    from pathlib import Path

    assert all(
        not path.read_bytes().startswith(b"\xef\xbb\xbf")
        for path in Path("fixtures").glob("round_*/*.html")
    )



def test_doctor_loads_delivery_settings_from_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DELIVERY_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DELIVERY_ADAPTER", raising=False)
    (tmp_path / ".env").write_text(
        "DELIVERY_ADAPTER=webhook\nDELIVERY_WEBHOOK_URL=https://hooks.example.test/token\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--fixture"])

    assert result.exit_code == 0
    assert "webhook adapter: configured" in result.stdout.lower()



def test_doctor_distinguishes_configured_url_from_selected_adapter(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DELIVERY_WEBHOOK_URL", "https://hooks.example.test/token")
    monkeypatch.setenv("DELIVERY_ADAPTER", "mock")

    result = runner.invoke(app, ["doctor", "--fixture"])

    assert result.exit_code == 0
    assert "webhook adapter: not selected" in result.stdout.lower()
