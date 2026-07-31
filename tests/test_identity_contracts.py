from datetime import UTC, datetime
import json

import pytest
from pydantic import ValidationError

from competitor_agent.config import ProjectConfig
from competitor_agent.discovery import discover_candidates, score_candidate
from competitor_agent.adapters.search import SearchResult, StaticSearchProvider
from competitor_agent.models import Candidate, CandidateStatus, Digest, Evidence, ProductSnapshot, ProjectionReceipt, RunResult, RunStatus
from competitor_agent.storage import StateStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def config() -> ProjectConfig:
    return ProjectConfig.model_validate({"project": {"id": "demo", "topic": "agent", "keywords": ["agent"]}})


def test_new_contract_fields_keep_old_json_compatible() -> None:
    candidate = Candidate.model_validate({
        "id": "candidate-1", "name": "Acme", "homepage": "https://acme.test",
        "category": "ai", "score": 0.8, "reasons": ["match"], "status": "monitored",
    })
    snapshot = ProductSnapshot.model_validate({
        "candidate_id": "candidate-1", "observed_at": NOW.isoformat(),
        "summary": "", "confidence": 0.8,
    })
    digest = Digest.model_validate({
        "run_id": "run-1", "kind": "baseline", "title": "Baseline", "summary": "", "report_path": "report.md",
    })
    result = RunResult.model_validate({
        "run_id": "run-1", "status": "success", "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(),
    })

    assert candidate.official_entry_urls == []
    assert snapshot.field_evidence == {}
    assert digest.base_links == {}
    assert digest.projection is None
    assert result.projection is None


def test_projection_receipt_and_field_path_evidence_are_serialized() -> None:
    receipt = ProjectionReceipt(adapter="feishu_base", synced=True, base_url="https://base.test/app", detail="ok")
    snapshot = ProductSnapshot(
        candidate_id="candidate-1", observed_at=NOW, summary="Acme", confidence=0.9,
        evidence=[Evidence(source_url="https://acme.test", excerpt="Acme", observed_at=NOW)], field_evidence={"summary": []},
    )
    digest = Digest(run_id="run-1", kind="baseline", title="Baseline", summary="", report_path="report.md", base_links={"candidate-1": "https://base.test/record"}, projection=receipt)
    assert snapshot.model_dump()["field_evidence"] == {"summary": []}
    assert digest.projection == receipt


def test_config_adds_collection_page_cap_projection_and_strict_feishu_base() -> None:
    defaults = config()
    assert defaults.collection.max_pages_per_candidate == 8
    assert defaults.adapters.projection == "mock"
    assert defaults.feishu_base is None

    configured = ProjectConfig.model_validate({
        "project": {"id": "demo", "topic": "agent", "keywords": ["agent"]},
        "feishu_base": {"app_token": "app-token", "competitors_table": "tbl_competitors", "snapshots_table": "tbl_snapshots", "changes_table": "tbl_changes", "runs_table": "tbl_runs"},
    })
    assert configured.feishu_base.app_token == "app-token"
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate({
            "project": {"id": "demo", "topic": "agent", "keywords": ["agent"]},
            "feishu_base": {"app_token": "app-token", "unexpected": "value"},
        })


def test_discovery_uses_registrable_domain_identity_origin_and_entry_urls() -> None:
    first = score_candidate("Acme", "https://www.acme.co.uk/pricing?utm_source=search", "ai", {"feature_overlap": 1}, config())
    second = score_candidate("Renamed", "https://docs.acme.co.uk/features", "ai", {"feature_overlap": 1}, config())
    assert first.id == second.id
    assert first.homepage == "https://acme.co.uk"
    assert first.official_entry_urls == ["https://www.acme.co.uk/pricing"]


def test_discovery_merges_duplicate_official_pages_by_domain() -> None:
    provider = StaticSearchProvider({"agent": [
        SearchResult("Acme pricing", "https://www.acme.co.uk/pricing", "agent"),
        SearchResult("Acme docs", "https://docs.acme.co.uk/features", "agent"),
    ]})
    candidates = discover_candidates(config(), provider)
    assert len(candidates) == 1
    assert candidates[0].homepage == "https://acme.co.uk"
    assert candidates[0].official_entry_urls == ["https://www.acme.co.uk/pricing", "https://docs.acme.co.uk/features"]


def legacy_candidate(candidate_id: str, homepage: str, name: str = "Acme") -> Candidate:
    return Candidate(id=candidate_id, name=name, homepage=homepage, category="ai", score=.8, reasons=["match"], status=CandidateStatus.MONITORED)


def test_identity_migration_preserves_history_aliases_and_is_idempotent(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        store._db.execute("INSERT INTO candidates(candidate_id, payload) VALUES (?, ?)", ("old-acme", legacy_candidate("old-acme", "https://www.acme.co.uk/pricing").model_dump_json()))
        store._db.execute("INSERT INTO snapshots(candidate_id, observed_at, payload) VALUES (?, ?, ?)", ("old-acme", NOW.isoformat(), ProductSnapshot(candidate_id="old-acme", observed_at=NOW, summary="", confidence=.8).model_dump_json()))
        store._db.execute("INSERT INTO changes(run_id, candidate_id, field_path, payload) VALUES (?, ?, ?, ?)", ("r", "old-acme", "summary", "{}"))
        store._db.execute("INSERT INTO missing_counts(candidate_id, field_path, missing_count) VALUES (?, ?, ?)", ("old-acme", "summary", 2))
        store._db.commit()

        store.migrate_candidate_identities()
        candidate_id = store._db.execute("SELECT candidate_id FROM candidates").fetchone()[0]
        assert candidate_id != "old-acme"
        assert store.get_latest_snapshot(candidate_id).candidate_id == candidate_id
        assert store._db.execute("SELECT candidate_id FROM changes").fetchone()[0] == candidate_id
        assert store.get_missing_count(candidate_id, "summary") == 2
        assert store.resolve_candidate_alias("old-acme") == candidate_id
        store.migrate_candidate_identities()
        assert store._db.execute("SELECT COUNT(*) FROM candidate_aliases").fetchone()[0] == 1


def test_initialize_automatically_migrates_persisted_legacy_candidate_ids(tmp_path) -> None:
    path = tmp_path / "state.sqlite"
    with StateStore(path) as store:
        old = legacy_candidate("legacy", "https://www.acme.test/pricing")
        store._db.execute("INSERT INTO candidates(candidate_id, payload) VALUES (?, ?)", (old.id, old.model_dump_json()))
        store._db.commit()
    with StateStore(path) as reopened:
        assert reopened.resolve_candidate_alias("legacy") != "legacy"


def test_identity_migration_resolves_same_domain_collisions_deterministically(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        for candidate_id, url in [("a", "https://www.acme.test/pricing"), ("b", "https://docs.acme.test/features")]:
            item = legacy_candidate(candidate_id, url, name="Zed" if candidate_id == "a" else "Alpha")
            store._db.execute("INSERT INTO candidates(candidate_id, payload) VALUES (?, ?)", (candidate_id, item.model_dump_json()))
        store._db.execute("INSERT INTO missing_counts(candidate_id, field_path, missing_count) VALUES (?, ?, ?)", ("a", "summary", 1))
        store._db.execute("INSERT INTO missing_counts(candidate_id, field_path, missing_count) VALUES (?, ?, ?)", ("b", "summary", 3))
        store._db.commit()
        store.migrate_candidate_identities()
        rows = store._db.execute("SELECT candidate_id, payload FROM candidates").fetchall()
        assert len(rows) == 1
        migrated = Candidate.model_validate_json(rows[0]["payload"])
        assert migrated.name == "Alpha"
        assert migrated.official_entry_urls == ["https://docs.acme.test/features", "https://www.acme.test/pricing"]
        assert store.resolve_candidate_alias("a") == rows[0]["candidate_id"]
        assert store.resolve_candidate_alias("b") == rows[0]["candidate_id"]
        assert store.get_missing_count(rows[0]["candidate_id"], "summary") == 3


def test_projection_resources_record_mappings_and_outbox_persist(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        store.set_projection_resource("competitors", "tbl_competitors")
        store.set_projection_record_mapping("competitors", "candidate-1", "rec_1")
        outbox_id = store.enqueue_projection_outbox("upsert", {"records": [{"id": "candidate-1"}]})
        assert store.get_projection_resource("competitors") == "tbl_competitors"
        assert store.get_projection_record_mapping("competitors", "candidate-1") == "rec_1"
        pending = store.list_projection_outbox()
        assert pending[0]["id"] == outbox_id
        assert json.loads(pending[0]["payload"])["records"][0]["id"] == "candidate-1"
        store.mark_projection_outbox_attempt(outbox_id, "temporary")

    with StateStore(tmp_path / "state.sqlite") as reopened:
        assert reopened.get_projection_record_mapping("competitors", "candidate-1") == "rec_1"
        assert reopened.list_projection_outbox()[0]["attempts"] == 1
