from datetime import UTC, datetime
import csv
import json

from competitor_agent.models import Candidate, CandidateStatus, ChangeEvent, ChangeImportance, Evidence, PriceTier, ProductSnapshot
from competitor_agent.reporting import build_digest, write_reports

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def candidate() -> Candidate:
    return Candidate(id="candidate-1", name="Acme", homepage="https://acme.test", category="ai", score=0.9, reasons=["match"], status=CandidateStatus.MONITORED)


def snapshot() -> ProductSnapshot:
    return ProductSnapshot(
        candidate_id="candidate-1",
        observed_at=NOW,
        summary="Agent workspace",
        features=["workflow automation", "dashboards"],
        configurations={"deployment": "cloud"},
        specifications={"requests_per_month": 1000},
        pricing=[PriceTier(name="Pro", amount=29, currency="USD", period="month")],
        confidence=0.9,
        evidence=[Evidence(source_url="https://acme.test/pricing", excerpt="Pro $29", observed_at=NOW)],
    )


def event(index: int) -> ChangeEvent:
    return ChangeEvent(candidate_id="candidate-1", field_path=f"pricing.Pro.{index}", before=index, after=index + 1, importance=ChangeImportance.HIGH)


def test_build_digest_labels_first_run_as_baseline_and_limits_message_changes(tmp_path) -> None:
    digest = build_digest("run-1", [candidate()], [snapshot()], [event(i) for i in range(6)], ["source failed"], tmp_path / "run-1.md", True)

    assert digest.kind == "baseline"
    assert "\u7ade\u54c1" in digest.title
    assert "\u5019\u9009" in digest.summary
    assert len(digest.changes) == 5
    assert digest.failed_sources == ["source failed"]
    assert digest.report_path.endswith("run-1.md")
    assert len(digest.products) == 1
    assert digest.products[0].name == "Acme"
    assert digest.products[0].features == ["workflow automation", "dashboards"]
    assert digest.products[0].pricing[0].name == "Pro"
    assert digest.products[0].configurations == {"deployment": "cloud"}
    assert digest.products[0].evidence_urls == ["https://acme.test/pricing"]


def test_build_digest_normalizes_url_names_and_bounds_card_fields(tmp_path) -> None:
    item = candidate().model_copy(
        update={"name": "https://www.acme.test/pricing", "homepage": "https://www.acme.test/pricing"}
    )
    snap = snapshot().model_copy(
        update={
            "features": [f"feature-{index}" for index in range(8)],
            "configurations": {f"key-{index}": f"value-{index}" for index in range(5)},
            "evidence": [
                Evidence(source_url=f"https://acme.test/source-{index}", excerpt="fact", observed_at=NOW)
                for index in range(4)
            ],
        }
    )

    digest = build_digest("run-2", [item], [snap], [], [], tmp_path / "run-2.md", True)

    assert digest.products[0].name == "acme.test"
    assert len(digest.products[0].features) == 5
    assert len(digest.products[0].configurations) == 3
    assert len(digest.products[0].evidence_urls) == 2


def test_write_reports_writes_full_markdown_json_and_csv(tmp_path) -> None:
    changes = [event(i) for i in range(6)]
    digest = build_digest("run-1", [candidate()], [snapshot()], changes, [], tmp_path / "report.md", False)
    paths = write_reports(digest, [candidate()], [snapshot()], changes, [], tmp_path)

    assert set(paths) == {"markdown", "json", "csv"}
    assert all(path.exists() for path in paths.values())
    assert len(json.loads(paths["json"].read_text(encoding="utf-8"))["changes"]) == 6
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "pricing.Pro.5" in markdown
    assert "deployment" in markdown
    assert "requests_per_month" in markdown
    assert "29.0" in markdown
    assert "https://acme.test/pricing" in markdown
    with paths["csv"].open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 7
    assert rows[0]["record_type"] == "snapshot"
    assert '"deployment": "cloud"' in rows[0]["configurations"]
    assert any(row["record_type"] == "change" for row in rows)
