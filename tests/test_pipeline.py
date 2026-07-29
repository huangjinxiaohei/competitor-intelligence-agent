from pathlib import Path

import yaml

from competitor_agent.delivery import MockDeliveryAdapter
from competitor_agent.models import CandidateStatus, RunStatus
from competitor_agent.pipeline import _delivery, discover_only, report_run, run_pipeline
from competitor_agent.storage import StateStore


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "project.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "id": "fixture-demo",
                    "topic": "Agent tools",
                    "keywords": ["agent", "automation"],
                },
                "storage": {
                    "database": str(tmp_path / "state.db"),
                    "reports_dir": str(tmp_path / "reports"),
                },
                "adapters": {
                    "search": "host",
                    "analyzer": "heuristic",
                    "delivery": "mock",
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def test_fixture_discovery_returns_two_monitored_candidates(tmp_path: Path) -> None:
    candidates = discover_only(_config(tmp_path), fixture=True)

    assert len(candidates) == 2
    assert all(item.status is CandidateStatus.MONITORED for item in candidates)
    assert {item.name for item in candidates} == {"NovaBoard", "OrbitNote"}


def test_fixture_pipeline_baseline_change_and_no_change_rounds(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    delivery = MockDeliveryAdapter()

    baseline = run_pipeline(config_path, fixture=True, delivery_adapter=delivery)
    changed = run_pipeline(config_path, fixture=True, delivery_adapter=delivery)
    unchanged = run_pipeline(config_path, fixture=True, delivery_adapter=delivery)

    assert baseline.status is RunStatus.SUCCESS
    assert baseline.digest is not None and baseline.digest.kind == "baseline"
    assert baseline.delivery is not None and baseline.delivery.delivered
    assert baseline.snapshot_count == 2

    assert changed.status is RunStatus.SUCCESS
    assert changed.change_count >= 2
    assert changed.digest is not None
    assert any(
        event.field_path.startswith(("pricing.", "configurations."))
        for event in changed.digest.changes
    )

    assert unchanged.status is RunStatus.SUCCESS
    assert unchanged.change_count == 0
    assert unchanged.delivery is None
    assert len(delivery.calls) == 2

    push_probe = MockDeliveryAdapter()
    forced = run_pipeline(
        config_path,
        fixture=True,
        delivery_adapter=push_probe,
        force_publish=True,
    )
    assert forced.change_count == 0
    assert forced.delivery is not None and forced.delivery.delivered
    assert len(push_probe.calls) == 1

    report_path = Path(report_run(config_path, baseline.run_id))
    assert report_path.is_file()
    assert report_path.suffix == ".md"


def test_collection_failure_preserves_last_successful_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = _config(tmp_path)
    baseline = run_pipeline(
        config_path,
        fixture=True,
        delivery_adapter=MockDeliveryAdapter(),
    )
    with StateStore(tmp_path / "state.db") as store:
        candidates = discover_only(config_path, fixture=True)
        before = {
            item.id: store.get_latest_snapshot(item.id)
            for item in candidates
        }

    def fail_collection(*_args, **_kwargs):
        raise TimeoutError("synthetic source timeout")

    monkeypatch.setattr("competitor_agent.pipeline.collect_candidate", fail_collection)
    failed = run_pipeline(
        config_path,
        fixture=True,
        delivery_adapter=MockDeliveryAdapter(),
    )

    assert baseline.snapshot_count == 2
    assert failed.status is RunStatus.PARTIAL
    assert failed.snapshot_count == 0
    assert failed.change_count == 0
    with StateStore(tmp_path / "state.db") as store:
        for candidate_id, snapshot in before.items():
            assert snapshot is not None
            assert store.get_latest_snapshot(candidate_id) == snapshot



def test_live_dry_run_does_not_collect_or_mutate_state(tmp_path: Path, monkeypatch) -> None:
    config_path = _config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["project"]["seed_urls"] = ["https://agent.example/pricing"]
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run attempted source collection")

    monkeypatch.setattr("competitor_agent.pipeline.collect_candidate", network_forbidden)
    result = run_pipeline(config_path, dry_run=True)

    assert result.status is RunStatus.SUCCESS
    assert result.candidate_count == 1
    assert result.snapshot_count == 0
    assert result.delivery is None
    assert not (tmp_path / "state.db").exists()
    assert not (tmp_path / "reports").exists()


def test_delivery_adapter_respects_environment_override(tmp_path: Path, monkeypatch) -> None:
    from competitor_agent.config import load_config
    from competitor_agent.delivery import WebhookDeliveryAdapter

    config = load_config(_config(tmp_path))
    monkeypatch.setenv("DELIVERY_ADAPTER", "webhook")
    monkeypatch.setenv("DELIVERY_WEBHOOK_URL", "https://hooks.example.test/token")

    assert isinstance(_delivery(config, None), WebhookDeliveryAdapter)


def test_pipeline_confirms_structured_deletion_on_second_missing_round(
    tmp_path: Path,
) -> None:
    config_path = _config(tmp_path)
    fixture_root = tmp_path / "fixtures"
    stable_orbit = """<html><head><title>OrbitNote</title></head><body>
    <p>Team plan $18 per month per member.</p><p>Configure region: US.</p>
    </body></html>"""
    nova_with_price = """<html><head><title>NovaBoard</title></head><body>
    <p>Pro plan $29 per month per editor.</p><p>Configure model: Standard.</p>
    </body></html>"""
    nova_without_price = """<html><head><title>NovaBoard</title></head><body>
    <p>Configure model: Standard.</p><p>Pricing details are temporarily omitted.</p>
    </body></html>"""
    for index, nova in enumerate(
        (nova_with_price, nova_without_price, nova_without_price), start=1
    ):
        round_dir = fixture_root / f"round_{index:02d}_test"
        round_dir.mkdir(parents=True)
        (round_dir / "novaboard.html").write_text(nova, encoding="utf-8")
        (round_dir / "orbitnote.html").write_text(stable_orbit, encoding="utf-8")

    delivery = MockDeliveryAdapter()
    baseline = run_pipeline(
        config_path, fixture=True, fixture_root=fixture_root, delivery_adapter=delivery
    )
    first_missing = run_pipeline(
        config_path, fixture=True, fixture_root=fixture_root, delivery_adapter=delivery
    )
    confirmed_missing = run_pipeline(
        config_path, fixture=True, fixture_root=fixture_root, delivery_adapter=delivery
    )

    assert baseline.change_count == 0
    assert first_missing.change_count == 0
    assert confirmed_missing.change_count > 0
    assert confirmed_missing.digest is not None
    assert any(
        event.field_path.casefold().startswith("pricing.")
        and event.after is None
        and event.confirmed
        for event in confirmed_missing.digest.changes
    )



def test_pipeline_selects_configured_http_json_analyzer(tmp_path: Path, monkeypatch) -> None:
    from competitor_agent.analyzer import analyze_documents

    config_path = _config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["adapters"]["analyzer"] = "http-json"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    calls: list[str] = []

    class FakeHttpAnalyzer:
        def analyze(self, candidate, documents):
            calls.append(candidate.id)
            return analyze_documents(candidate, documents)

    monkeypatch.setattr("competitor_agent.pipeline.HttpJsonAnalyzer", FakeHttpAnalyzer)
    result = run_pipeline(
        config_path,
        fixture=True,
        delivery_adapter=MockDeliveryAdapter(),
    )

    assert result.snapshot_count == 2
    assert len(calls) == 2
