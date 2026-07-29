from datetime import UTC, datetime, timedelta

from competitor_agent.models import (
    Candidate,
    CandidateStatus,
    ChangeEvent,
    ChangeImportance,
    ProductSnapshot,
    RunResult,
    RunStatus,
)
from competitor_agent.storage import StateStore


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def candidate() -> Candidate:
    return Candidate(
        id="candidate-1", name="Acme", homepage="https://acme.test",
        category="ai", score=0.9, reasons=["match"], status=CandidateStatus.MONITORED,
    )


def snapshot(hour: int = 0) -> ProductSnapshot:
    return ProductSnapshot(
        candidate_id="candidate-1", observed_at=NOW + timedelta(hours=hour),
        summary="", confidence=0.8,
    )


def test_state_store_persists_models_locks_and_missing_counts(tmp_path) -> None:
    db_path = tmp_path / "state.sqlite"
    result = RunResult(
        run_id="run-1", status=RunStatus.SUCCESS, started_at=NOW,
        finished_at=NOW + timedelta(minutes=1), candidate_count=1,
        snapshot_count=1, change_count=1,
    )
    event = ChangeEvent(
        candidate_id="candidate-1", field_path="specifications.memory",
        before="8GB", after="16GB", importance=ChangeImportance.HIGH,
    )

    with StateStore(db_path) as store:
        store.initialize()
        assert store.acquire_run_lock("project-1") is True
        assert store.acquire_run_lock("project-1") is False
        store.release_run_lock("project-1")
        assert store.acquire_run_lock("project-1") is True
        store.save_candidates([candidate()])
        store.start_run("run-1", NOW)
        store.save_snapshot(snapshot())
        store.save_changes("run-1", [event])
        store.set_missing_count("candidate-1", "specifications.memory", 2)
        store.finish_run(result)
        assert store.get_latest_snapshot("candidate-1") == snapshot()
        assert store.get_missing_count("candidate-1", "specifications.memory") == 2
        runs = store.list_runs()

    assert runs[0]["run_id"] == "run-1"
    assert runs[0]["status"] == "success"


def test_state_store_latest_snapshot_is_selected_by_timestamp(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        store.save_snapshot(snapshot(1))
        store.save_snapshot(snapshot(2))
        assert store.get_latest_snapshot("candidate-1") == snapshot(2)


def test_state_store_reclaims_stale_locks_but_keeps_active_locks(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        assert store.acquire_run_lock("active") is True
        assert store.acquire_run_lock("active", lock_ttl_seconds=21_600) is False
        store._db.execute(
            "INSERT INTO run_locks(project_id, acquired_at) VALUES (?, ?)",
            ("stale", "2000-01-01T00:00:00+00:00"),
        )
        store._db.commit()
        assert store.acquire_run_lock("stale") is True
