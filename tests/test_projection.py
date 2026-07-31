from __future__ import annotations

from datetime import UTC, datetime, timedelta

from competitor_agent.models import (
    Candidate, CandidateStatus, ChangeEvent, ChangeImportance, Evidence,
    PriceTier, ProductSnapshot, RunResult, RunStatus,
)
from competitor_agent.feishu_base import TABLES
from competitor_agent.projection import FeishuBaseProjection, price_key
from competitor_agent.storage import StateStore

NOW = datetime(2026, 7, 31, tzinfo=UTC)


class FakeBase:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, dict]] = {key: {} for key in ("tc", "tp", "te", "tr")}
        self.create_batches: list[tuple[str, int]] = []
        self.update_batches: list[tuple[str, int, list[dict]]] = []
        self.fail_once = False

    def list_records(self, _app: str, table: str) -> list[dict]:
        return list(self.records[table].values())

    def create_records(self, _app: str, table: str, records: list[dict]) -> list[dict]:
        if self.fail_once:
            self.fail_once = False
            from competitor_agent.feishu_base import FeishuBaseError
            raise FeishuBaseError("temporary", operation="projection create")
        self.create_batches.append((table, len(records)))
        result = []
        for record in records:
            record_id = f"rec-{table}-{len(self.records[table]) + 1}"
            stored = {"record_id": record_id, "fields": dict(record["fields"])}
            self.records[table][record_id] = stored
            result.append(stored)
        return result

    def update_records(self, _app: str, table: str, records: list[dict]) -> list[dict]:
        self.update_batches.append((table, len(records), records))
        for record in records:
            self.records[table][record["record_id"]]["fields"].update(record["fields"])
        return records


def candidate(index: int = 1) -> Candidate:
    return Candidate(id=f"candidate-{index}", name=f"Acme {index}", homepage=f"https://acme{index}.test",
                     category="saas", score=.8, reasons=["match"], status=CandidateStatus.MONITORED)


def snapshot(index: int = 1, amount: float | None = 10, qualifiers: list[str] | None = None) -> ProductSnapshot:
    evidence = Evidence(source_url=f"https://acme{index}.test/pricing", excerpt="Pro monthly $10", observed_at=NOW)
    return ProductSnapshot(candidate_id=f"candidate-{index}", observed_at=NOW, summary="A summary",
                           features=["Board", "Duplicate footer"], configurations={"seats": 5},
                           pricing=[PriceTier(name="Pro", amount=amount, currency="USD", period="month", unit="seat", qualifiers=qualifiers or [])],
                           confidence=.9, evidence=[evidence])


def result(run_id: str = "run-1") -> RunResult:
    return RunResult(run_id=run_id, status=RunStatus.SUCCESS, started_at=NOW,
                     finished_at=NOW + timedelta(seconds=3), candidate_count=1, snapshot_count=1)


def provision(store: StateStore) -> None:
    store.set_projection_resource("base", "app", "https://base.test/app")
    for key, table in {"competitors": "tc", "pricing": "tp", "changes": "te", "runs": "tr"}.items():
        store.set_projection_resource(f"table:{key}", table)
    store.set_projection_resource("view:competitors:竞品总览", "vc", "https://base.test/overview")
    store.set_projection_resource("view:changes:本周变化", "ve", "https://base.test/changes")


def test_resync_is_idempotent_preserves_human_fields_and_price_amount_updates(tmp_path) -> None:
    api = FakeBase()
    with StateStore(tmp_path / "state.sqlite") as store:
        provision(store); store.save_candidates([candidate()]); store.save_snapshot(snapshot()); store.finish_run(result())
        projection = FeishuBaseProjection(api, store)
        first = projection.resync()
        assert first.synced and first.records_synced == 3
        assert first.resource_links["\u7ade\u54c1\u603b\u89c8"] == "https://base.test/overview"
        competitor = next(iter(api.records["tc"].values()))
        competitor["fields"]["人工关注级别"] = "critical"
        # Same tier identity, updated amount: update existing row rather than create a duplicate.
        store.save_snapshot(snapshot(amount=12)); projection.resync()
        assert len(api.records["tp"]) == 1
        assert next(iter(api.records["tp"].values()))["fields"]["金额"] == 12
        assert competitor["fields"]["人工关注级别"] == "critical"
        assert all("人工关注级别" not in record["fields"] for _, _, batch in api.update_batches for record in batch)


def test_relations_batched_writes_confirmed_removal_and_mapping_recovery(tmp_path) -> None:
    api = FakeBase()
    with StateStore(tmp_path / "state.sqlite") as store:
        provision(store)
        candidates = [candidate(i) for i in range(1, 202)]
        store.save_candidates(candidates)
        for i in range(1, 202):
            store.save_snapshot(snapshot(i))
        projection = FeishuBaseProjection(api, store)
        projection.resync()
        assert api.create_batches[:2] == [("tc", 200), ("tc", 1)]
        price = next(iter(api.records["tp"].values()))
        assert price["fields"]["关联竞品"] == ["rec-tc-1"]
        # Delete current durable tier after confirmation: its mapped record becomes inactive.
        store.save_snapshot(ProductSnapshot(candidate_id="candidate-1", observed_at=NOW + timedelta(days=1), summary="", confidence=.8))
        projection.resync()
        assert price["fields"]["有效状态"] is False
        # Simulate lost local mappings; rows are recovered by durable business key, not duplicated.
        store._db.execute("DELETE FROM projection_record_mappings"); store._db.commit()
        before = {table: len(rows) for table, rows in api.records.items()}
        projection.resync()
        assert {table: len(rows) for table, rows in api.records.items()} == before


def test_changes_runs_and_outbox_replay_are_idempotent(tmp_path) -> None:
    api = FakeBase()
    with StateStore(tmp_path / "state.sqlite") as store:
        provision(store); store.save_candidates([candidate()]); store.save_snapshot(snapshot())
        event = ChangeEvent(candidate_id="candidate-1", field_path="pricing.Pro.amount", before=10, after=12,
                            importance=ChangeImportance.HIGH, evidence=snapshot().evidence)
        store.save_changes("run-1", [event]); store.finish_run(result("run-1")); store.finish_run(result("run-2"))
        api.fail_once = True
        projection = FeishuBaseProjection(api, store)
        failed = projection.resync()
        assert not failed.synced and failed.outbox_pending == 1
        replayed = projection.resync()
        assert replayed.synced and replayed.outbox_pending == 0
        assert len(api.records["te"]) == 1 and len(api.records["tr"]) == 2
        latest = [item for item in api.records["tr"].values() if item["fields"]["是否最新"]]
        assert latest[0]["fields"]["运行 ID"] == "run-2"
        # Replaying a whole rebuild does not duplicate append-only facts.
        projection.resync()
        assert len(api.records["te"]) == 1 and len(api.records["tr"]) == 2


def test_missing_base_resources_is_classified_without_network(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        receipt = FeishuBaseProjection(FakeBase(), store).resync()
    assert not receipt.synced and "base-setup" in receipt.detail


def test_price_key_distinguishes_period_but_not_amount() -> None:
    monthly = PriceTier(name="Pro", amount=10, period="month", unit="seat")
    changed = PriceTier(name="Pro", amount=12, period="month", unit="seat")
    annual = PriceTier(name="Pro", amount=100, period="year", unit="seat")
    qualifier_changed = PriceTier(name="Pro", amount=10, period="month", unit="seat", qualifiers=["annual promotion"])
    assert price_key("c", monthly) == price_key("c", changed)
    assert price_key("c", monthly) == price_key("c", qualifier_changed)
    assert price_key("c", monthly) != price_key("c", annual)


def test_missing_numeric_price_is_explicitly_labeled_without_guessing(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        projection = FeishuBaseProjection(FakeBase(), store)
        sales = PriceTier(name="Enterprise", amount=None, qualifiers=["contact-sales"])
        unknown = PriceTier(name="Custom", amount=None)
        sales_fields = projection._price_fields("one", sales, snapshot(), "rec-competitor")
        unknown_fields = projection._price_fields("two", unknown, snapshot(), "rec-competitor")
        # Use the declared Base field order so this test remains encoding-neutral.
        ordered = next(table for table in TABLES if table.key == "pricing").fields
        amount, qualifiers = ordered[3].name, ordered[7].name
        assert sales_fields[amount] is None
        assert sales_fields[qualifiers] == "\u8054\u7cfb\u9500\u552e"
        assert unknown_fields[qualifiers] == "\u6682\u672a\u8bc6\u522b"
