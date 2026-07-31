from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from competitor_agent.cli import app
from competitor_agent.delivery import MockDeliveryAdapter, render_payload
from competitor_agent.models import Digest, DigestProduct, ProjectionReceipt, RunStatus
from competitor_agent.pipeline import base_doctor, run_pipeline
from competitor_agent.storage import StateStore


class FakeProjection:
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.outcomes = outcomes or [True]
        self.calls = 0
        self.runs = []

    def sync(self, run=None) -> ProjectionReceipt:
        self.runs.append(run)
        synced = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        return ProjectionReceipt(
            adapter="feishu-base", synced=synced, base_url="https://base.test/app",
            resource_links={
                "\u7ade\u54c1\u603b\u89c8": "https://base.test/overview",
                "\u672c\u5468\u53d8\u5316": "https://base.test/changes",
                "\u4ef7\u683c\u5bf9\u6bd4": "https://base.test/prices",
            } if synced else {},
            outbox_pending=0 if synced else 1,
            detail="ok" if synced else "deferred",
        )

    def competitor_record_links(self, candidate_ids):
        if not self.outcomes[min(max(self.calls - 1, 0), len(self.outcomes) - 1)]:
            return {}
        return {candidate_id: f"https://base.test/record/{candidate_id}" for candidate_id in candidate_ids}


def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump({
        "project": {"id": "task-five", "topic": "Agent tools", "keywords": ["agent"]},
        "storage": {"database": str(tmp_path / "state.db"), "reports_dir": str(tmp_path / "reports")},
        "adapters": {"analyzer": "heuristic", "delivery": "mock", "projection": "feishu-base"},
        "feishu_base": {"enabled": True, "base_name": "Test", "manifest_path": str(tmp_path / "manifest.json")},
    }, allow_unicode=True), encoding="utf-8")
    return path


def _report(result):
    assert result.digest is not None
    return json.loads(Path(result.digest.report_path).with_suffix(".json").read_text(encoding="utf-8"))


def test_pipeline_projects_then_delivers_baseline_and_skips_no_change_webhook(tmp_path: Path) -> None:
    config = config_path(tmp_path)
    projection = FakeProjection()
    delivery = MockDeliveryAdapter()
    baseline = run_pipeline(config, fixture=True, projection_adapter=projection, delivery_adapter=delivery)
    changed = run_pipeline(config, fixture=True, projection_adapter=projection, delivery_adapter=delivery)
    unchanged = run_pipeline(config, fixture=True, projection_adapter=projection, delivery_adapter=delivery)
    assert baseline.projection is not None and baseline.projection.synced
    assert baseline.digest is not None and baseline.digest.base_links["\u7ade\u54c1\u603b\u89c8"] == "https://base.test/overview"
    assert all(f"record:{product.candidate_id}" in baseline.digest.base_links for product in baseline.digest.products)
    assert changed.change_count > 0
    assert unchanged.change_count == 0 and unchanged.delivery is None
    assert len(delivery.calls) == 2
    assert projection.calls == 6
    for index in (0, 2, 4):
        assert projection.runs[index] is not None and projection.runs[index].delivery is None
    for index in (1, 3):
        assert projection.runs[index] is not None and projection.runs[index].delivery is not None
    assert baseline.digest.projection == baseline.projection
    payload = _report(baseline)
    assert payload["run_result"]["delivery"]["delivered"] is True
    assert payload["run_result"]["projection"] == baseline.projection.model_dump(mode="json")
    assert payload["digest"] == baseline.digest.model_dump(mode="json")
    markdown = Path(baseline.digest.report_path).read_text(encoding="utf-8")
    assert "## \u6700\u7ec8\u8fd0\u884c\u56de\u6267" in markdown
    assert "Base \u540c\u6b65: synced" in markdown


def test_failed_first_projection_marks_run_partial_keeps_evidence_notification_without_base_link(tmp_path: Path) -> None:
    config = config_path(tmp_path)
    delivery = MockDeliveryAdapter()
    run_pipeline(config, fixture=True, projection_adapter=FakeProjection(), delivery_adapter=delivery)
    failed = run_pipeline(config, fixture=True, projection_adapter=FakeProjection([False]), delivery_adapter=delivery)
    assert failed.status is RunStatus.PARTIAL
    assert failed.source_failures == []
    assert failed.projection_diagnostics
    assert failed.delivery is not None and failed.delivery.delivered
    assert failed.digest is not None and failed.digest.base_links == {}
    text = str(render_payload(failed.digest))
    assert "base.test/overview" not in text
    assert "Base\u540c\u6b65\u5f85\u91cd\u8bd5" in text
    assert "\u6765\u6e90\u5931\u8d25 0 \u4e2a" in failed.digest.summary
    markdown = Path(failed.digest.report_path).read_text(encoding="utf-8")
    assert "## 运行诊断" in markdown
    assert "## 采集失败" not in markdown
    with StateStore(tmp_path / "state.db") as store:
        assert store.list_finished_run_results()[-1].status is RunStatus.PARTIAL


def test_final_projection_failure_clears_digest_links_and_preserves_evidence(tmp_path: Path) -> None:
    projection = FakeProjection([True, False])
    result = run_pipeline(config_path(tmp_path), fixture=True, projection_adapter=projection, delivery_adapter=MockDeliveryAdapter())
    assert projection.calls == 2
    assert projection.runs[1] is not None and projection.runs[1].delivery is not None
    assert result.status is RunStatus.PARTIAL
    assert result.source_failures == []
    assert result.projection_diagnostics
    assert result.projection is not None and not result.projection.synced
    assert result.digest is not None and result.digest.projection == result.projection
    assert result.digest.base_links == {}
    text = str(render_payload(result.digest))
    assert "base.test/overview" not in text
    assert "\u6765\u6e90\u5931\u8d25 0 \u4e2a" in result.digest.summary
    assert "Base\u540c\u6b65\u5f85\u91cd\u8bd5" in text
    payload = _report(result)
    assert payload["run_result"]["status"] == "partial"
    assert payload["digest"]["base_links"] == {}


def test_card_renders_base_overview_changes_prices_and_product_record_links() -> None:
    digest = Digest(run_id="r", kind="baseline", title="\u60c5\u62a5", summary="\u6458\u8981", report_path="reports/r.md", base_links={"\u7ade\u54c1\u603b\u89c8": "https://base.test/overview", "\u672c\u5468\u53d8\u5316": "https://base.test/changes", "\u4ef7\u683c\u5bf9\u6bd4": "https://base.test/prices", "record:a": "https://base.test/record/a"}, products=[DigestProduct(candidate_id="a", name="A", homepage="https://a.test")])
    text = str(render_payload(digest))
    for label in ("\u7ade\u54c1\u603b\u89c8", "\u672c\u5468\u53d8\u5316", "\u4ef7\u683c\u5bf9\u6bd4", "Base\u8bb0\u5f55"):
        assert label in text


class DoctorClient:
    app_id = "app-id"
    app_secret = "app-secret"

    def __init__(self) -> None:
        self.listed: list[str] = []

    def tenant_token(self) -> str:
        return "token"

    def list_tables(self, app_token: str) -> list[dict]:
        self.listed.append(app_token)
        return []


def test_base_doctor_is_readonly_and_qualifies_unverified_capabilities(tmp_path: Path) -> None:
    config = config_path(tmp_path)
    database = tmp_path / "state.db"
    result = base_doctor(config, base_client=DoctorClient())
    assert not result["ok"]
    assert result["checks"]["authentication"] == "ok"
    assert "unverified" in result["checks"]["bitable_read"]
    assert not database.exists()


def test_base_doctor_reads_existing_projection_without_mutating_sqlite(tmp_path: Path) -> None:
    config = config_path(tmp_path)
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        store.set_projection_resource("base", "app-existing")
    before = database.read_bytes()
    api = DoctorClient()
    result = base_doctor(config, base_client=api)
    assert not result["ok"] and result["checks"]["bitable_read"] == "ok"
    assert "unverified" in result["checks"]["bitable_write"]
    assert "unverified" in result["checks"]["collaborator_manage"]
    assert api.listed == ["app-existing"]
    assert database.read_bytes() == before

def test_base_cli_commands_forward_config(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    received: list[str] = []
    def fake(name):
        def invoke(**kwargs):
            received.append(name)
            return {"ok": True, "config": str(kwargs["config_path"])}
        return invoke
    monkeypatch.setattr("competitor_agent.cli._pipeline_function", fake)
    for command in ("base-doctor", "base-setup", "base-resync"):
        result = runner.invoke(app, [command, "--config", str(tmp_path / "project.yaml")])
        assert result.exit_code == 0
        assert '"ok": true' in result.stdout
    assert received == ["base_doctor", "base_setup", "base_resync"]


def test_fixture_hard_isolation_ignores_hostile_environment(tmp_path: Path, monkeypatch) -> None:
    config = config_path(tmp_path)
    raw = yaml.safe_load(config.read_text(encoding="utf-8")); raw["adapters"]["analyzer"] = "hybrid-openai"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    for name in ("MODEL_API_URL", "MODEL_API_KEY", "MODEL_NAME", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "DELIVERY_ADAPTER", "DELIVERY_WEBHOOK_URL"):
        monkeypatch.setenv(name, "hostile-parent-value")
    result = run_pipeline(config, fixture=True)
    assert result.status is RunStatus.SUCCESS
    assert result.projection is None
    assert result.delivery is not None and result.delivery.adapter == "mock"
