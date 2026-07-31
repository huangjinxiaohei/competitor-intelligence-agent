"""Idempotent SQLite-to-Feishu Base projection.

The projection intentionally knows no collection or delivery details: SQLite is
always the fact store, while a Base can be rebuilt entirely from persisted facts.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC
import hashlib
import json
from typing import Any, Protocol

from .feishu_base import FeishuBaseError, TABLES
from .models import Candidate, ChangeEvent, ProductSnapshot, ProjectionReceipt, RunResult
from .storage import StateStore

_BATCH_SIZE = 200


class ProjectionAdapter(Protocol):
    """Projection boundary kept independent from chat message delivery."""

    def sync(self, run: RunResult | None = None) -> ProjectionReceipt: ...


def _field(table_key: str, english: str) -> str:
    """Use table order as the schema's one source of display-field truth."""
    fields = {"competitors": {
        "name": 0, "id": 1, "homepage": 2, "status": 3, "score": 4,
        "summary": 5, "features": 6, "configurations": 7, "availability": 8,
        "confidence": 9, "evidence": 10, "observed_at": 11,
    }, "pricing": {
        "key": 0, "competitor": 1, "name": 2, "amount": 3, "currency": 4,
        "period": 5, "unit": 6, "qualifiers": 7, "active": 8,
        "observed_at": 9, "evidence": 10,
    }, "changes": {
        "id": 0, "competitor": 1, "path": 2, "before": 3, "after": 4,
        "importance": 5, "detected_at": 6, "evidence": 7, "run_id": 8,
    }, "runs": {
        "id": 0, "status": 1, "duration": 2, "candidates": 3, "snapshots": 4,
        "changes": 5, "failed": 6, "model_degraded": 7, "base_sync": 8,
        "message_status": 9, "report_path": 10, "latest": 11,
    }}
    spec = next(spec for spec in TABLES if spec.key == table_key)
    return spec.fields[fields[table_key][english]].name


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _evidence(snapshot_or_event: ProductSnapshot | ChangeEvent) -> str:
    return "\n".join(
        f"{item.source_url} — {item.excerpt}" for item in snapshot_or_event.evidence
    )


def price_key(candidate_id: str, tier: Any) -> str:
    """Stable product-price identity; amount is deliberately excluded.

    A named monthly and yearly variant gets a distinct key through ``period``;
    qualifiers distinguish otherwise identical negotiated or regional variants.
    """
    parts = (candidate_id, tier.name.strip().casefold(), (tier.period or "").strip().casefold(),
             (tier.unit or "").strip().casefold())
    return "price:" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


def event_key(run_id: str, event: ChangeEvent) -> str:
    payload = _compact({"run_id": run_id, "candidate_id": event.candidate_id,
                        "path": event.field_path, "before": event.before, "after": event.after,
                        "detected_at": event.detected_at.isoformat()})
    return "event:" + hashlib.sha256(payload.encode()).hexdigest()[:24]


class FeishuBaseProjection:
    """Synchronise one initialized Base from a :class:`StateStore`."""

    def __init__(self, client: Any, store: StateStore) -> None:
        self.client = client
        self.store = store

    def sync(self, run: RunResult | None = None) -> ProjectionReceipt:
        resources = self._resources()
        if resources is None:
            return self._receipt(False, 0, "Base schema resources are missing; run base-setup first.")
        app_token, tables = resources
        synced = self._drain_outbox(app_token, tables)
        count = 0
        try:
            candidates = self.store.list_candidates()
            snapshots = {item.candidate_id: item for item in self.store.list_latest_snapshots()}
            count += self._sync_competitors(app_token, tables["competitors"], candidates, snapshots)
            competitor_ids = self.store.list_projection_record_mappings("record:competitors")
            count += self._sync_prices(app_token, tables["pricing"], snapshots, competitor_ids)
            count += self._sync_changes(app_token, tables["changes"], competitor_ids)
            count += self._sync_runs(app_token, tables["runs"])
        except FeishuBaseError as exc:
            synced = False
            return self._receipt(False, count, f"Base projection deferred: {exc.operation}.")
        return self._receipt(synced and not self.store.list_projection_outbox(), count,
                             "Base projection synchronized." if synced else "Base projection has deferred batches.")

    def resync(self) -> ProjectionReceipt:
        """Rebuild the remote projection only; no collection work is triggered."""
        return self.sync()

    def competitor_record_links(self, candidate_ids: Iterable[str]) -> dict[str, str]:
        base = self.store.get_projection_resource_link("base")
        table = self.store.get_projection_resource("table:competitors")
        if not base or not table:
            return {}
        return {
            candidate_id: f"{base}?table={table}&record={record_id}"
            for candidate_id in candidate_ids
            if (record_id := self.store.get_projection_record_mapping("record:competitors", candidate_id))
        }

    def _resources(self) -> tuple[str, dict[str, str]] | None:
        app_token = self.store.get_projection_resource("base")
        tables = {key: self.store.get_projection_resource(f"table:{key}")
                  for key in ("competitors", "pricing", "changes", "runs")}
        if not app_token or any(not value for value in tables.values()):
            return None
        return app_token, {key: str(value) for key, value in tables.items()}

    def _links(self) -> tuple[str | None, dict[str, str]]:
        base = self.store.get_projection_resource_link("base")
        links: dict[str, str] = {}
        for key in ("竞品总览", "本周变化", "价格对比", "运行历史"):
            link = self.store.get_projection_resource_link(
                next((resource for resource in self._view_resources() if resource.endswith(f":{key}")), "")
            )
            if link:
                links[key] = link
        return base, links

    @staticmethod
    def _view_resources() -> tuple[str, ...]:
        return (
            "view:competitors:\u7ade\u54c1\u603b\u89c8", "view:changes:\u672c\u5468\u53d8\u5316",
            "view:pricing:\u4ef7\u683c\u5bf9\u6bd4", "view:runs:\u8fd0\u884c\u5386\u53f2",
        )

    def _receipt(self, synced: bool, count: int, detail: str) -> ProjectionReceipt:
        base, links = self._links()
        return ProjectionReceipt(adapter="feishu-base", synced=synced, base_url=base,
                                 resource_links=links, records_synced=count,
                                 outbox_pending=len(self.store.list_projection_outbox()), detail=detail)

    def _machine_competitor_fields(self, candidate: Candidate, snapshot: ProductSnapshot | None) -> dict[str, Any]:
        fields: dict[str, Any] = {
            _field("competitors", "name"): candidate.name,
            _field("competitors", "id"): candidate.id,
            _field("competitors", "homepage"): candidate.homepage,
            _field("competitors", "status"): candidate.status.value,
            _field("competitors", "score"): candidate.score,
        }
        if snapshot is not None:
            fields.update({
                _field("competitors", "summary"): snapshot.summary,
                _field("competitors", "features"): "\n".join(snapshot.features),
                _field("competitors", "configurations"): _compact(snapshot.configurations),
                _field("competitors", "availability"): snapshot.availability or "",
                _field("competitors", "confidence"): snapshot.confidence,
                _field("competitors", "evidence"): _evidence(snapshot),
                _field("competitors", "observed_at"): _timestamp(snapshot.observed_at),
            })
        return fields

    def _sync_competitors(self, app: str, table: str, candidates: list[Candidate], snapshots: dict[str, ProductSnapshot]) -> int:
        return self._upsert(app, "competitors", table, [
            (item.id, self._machine_competitor_fields(item, snapshots.get(item.id))) for item in candidates
        ])

    def _price_fields(self, key: str, tier: Any, snapshot: ProductSnapshot, competitor_record_id: str) -> dict[str, Any]:
        qualifiers = [
            "\u8054\u7cfb\u9500\u552e"
            if str(item).strip().casefold().replace("-", " ") in {"contact sales", "contact sale"}
            else str(item)
            for item in tier.qualifiers
        ]
        if tier.amount is None and not qualifiers:
            qualifiers = ["\u6682\u672a\u8bc6\u522b"]
        fields: dict[str, Any] = {
            _field("pricing", "key"): key,
            _field("pricing", "competitor"): [competitor_record_id],
            _field("pricing", "name"): tier.name,
            _field("pricing", "currency"): tier.currency or "",
            _field("pricing", "period"): tier.period or "",
            _field("pricing", "unit"): tier.unit or "",
            _field("pricing", "qualifiers"): "；".join(qualifiers),
            _field("pricing", "active"): True,
            _field("pricing", "observed_at"): _timestamp(snapshot.observed_at),
            _field("pricing", "evidence"): _evidence(snapshot),
        }
        fields[_field("pricing", "amount")] = tier.amount
        return fields

    def _sync_prices(self, app: str, table: str, snapshots: dict[str, ProductSnapshot], competitor_ids: dict[str, str]) -> int:
        current: dict[str, dict[str, Any]] = {}
        for candidate_id, snapshot in snapshots.items():
            record_id = competitor_ids.get(candidate_id)
            if not record_id:
                continue
            for tier in snapshot.pricing:
                key = price_key(candidate_id, tier)
                current[key] = self._price_fields(key, tier, snapshot, record_id)
        count = self._upsert(app, "pricing", table, list(current.items()))
        # Pending removal facts stay in the durable snapshot. Once the diff layer
        # confirms removal, the tier disappears and its existing Base row flips off.
        inactive = [
            (key, {_field("pricing", "active"): False})
            for key in self.store.list_projection_record_mappings("record:pricing")
            if key not in current
        ]
        return count + self._upsert(app, "pricing", table, inactive, create_missing=False)

    def _sync_changes(self, app: str, table: str, competitor_ids: dict[str, str]) -> int:
        records: list[tuple[str, dict[str, Any]]] = []
        for run_id, event in self.store.list_confirmed_changes():
            competitor = competitor_ids.get(event.candidate_id)
            if not competitor:
                continue
            key = event_key(run_id, event)
            records.append((key, {
                _field("changes", "id"): key,
                _field("changes", "competitor"): [competitor],
                _field("changes", "path"): event.field_path,
                _field("changes", "before"): _compact(event.before),
                _field("changes", "after"): _compact(event.after),
                _field("changes", "importance"): event.importance.value,
                _field("changes", "detected_at"): _timestamp(event.detected_at),
                _field("changes", "evidence"): _evidence(event),
                _field("changes", "run_id"): run_id,
            }))
        return self._upsert(app, "changes", table, records)

    def _sync_runs(self, app: str, table: str) -> int:
        finished = self.store.list_finished_run_results()
        latest = finished[-1].run_id if finished else ""
        records: list[tuple[str, dict[str, Any]]] = []
        for result in finished:
            duration = max(0.0, (result.finished_at - result.started_at).total_seconds())
            records.append((result.run_id, {
                _field("runs", "id"): result.run_id,
                _field("runs", "status"): result.status.value,
                _field("runs", "duration"): duration,
                _field("runs", "candidates"): result.candidate_count,
                _field("runs", "snapshots"): result.snapshot_count,
                _field("runs", "changes"): result.change_count,
                _field("runs", "failed"): len(result.errors),
                _field("runs", "model_degraded"): any("model" in error.casefold() or "degrad" in error.casefold() for error in result.errors),
                _field("runs", "base_sync"): "synced",
                _field("runs", "message_status"): "sent" if result.delivery and result.delivery.delivered else "not_sent",
                _field("runs", "report_path"): result.digest.report_path if result.digest else "",
                _field("runs", "latest"): result.run_id == latest,
            }))
        return self._upsert(app, "runs", table, records)

    def _upsert(self, app: str, table_key: str, table: str, entries: list[tuple[str, dict[str, Any]]], *, create_missing: bool = True) -> int:
        if not entries:
            return 0
        mapping_key = f"record:{table_key}"
        mappings = self.store.list_projection_record_mappings(mapping_key)
        missing = [key for key, _ in entries if key not in mappings]
        if missing:
            recovered = self._recover_mappings(app, table_key, table, missing)
            mappings.update(recovered)
        creates = [(key, fields) for key, fields in entries if key not in mappings]
        updates = [(key, fields) for key, fields in entries if key in mappings]
        count = 0
        if create_missing:
            for batch in self._chunks(creates):
                if not self._perform("create", app, table_key, table, batch):
                    continue
                count += len(batch)
        for batch in self._chunks(updates):
            if self._perform("update", app, table_key, table, batch, mappings):
                count += len(batch)
        return count

    @staticmethod
    def _chunks(items: list[tuple[str, dict[str, Any]]]) -> Iterable[list[tuple[str, dict[str, Any]]]]:
        for index in range(0, len(items), _BATCH_SIZE):
            yield items[index:index + _BATCH_SIZE]

    def _recover_mappings(self, app: str, table_key: str, table: str, keys: list[str]) -> dict[str, str]:
        primary = _field(table_key, "id" if table_key in {"changes", "runs"} else "key" if table_key == "pricing" else "id")
        existing = self.client.list_records(app, table)
        recovered: dict[str, str] = {}
        wanted = set(keys)
        for record in existing:
            fields = record.get("fields", {})
            record_id = record.get("record_id")
            key = fields.get(primary) if isinstance(fields, dict) else None
            if key in wanted and isinstance(record_id, str):
                recovered[str(key)] = record_id
                self.store.set_projection_record_mapping(f"record:{table_key}", str(key), record_id)
        return recovered

    def _perform(self, kind: str, app: str, table_key: str, table: str,
                 entries: list[tuple[str, dict[str, Any]]], mappings: dict[str, str] | None = None) -> bool:
        operation = {
            "kind": kind,
            "table_key": table_key,
            "records": [{"business_key": key, "fields": fields} for key, fields in entries],
        }
        try:
            self._execute(operation, mappings)
            return True
        except FeishuBaseError as exc:
            outbox_id = self.store.enqueue_projection_outbox("base-write", operation)
            self.store.mark_projection_outbox_attempt(outbox_id, exc.operation)
            return False

    def _execute(self, operation: dict[str, Any], mappings: dict[str, str] | None = None) -> None:
        kind = str(operation["kind"])
        table_key = str(operation["table_key"])
        table = self.store.get_projection_resource(f"table:{table_key}")
        app = self.store.get_projection_resource("base")
        if not table or not app:
            raise FeishuBaseError("Base schema resources are missing.", operation="projection outbox")
        records = operation.get("records", [])
        if not isinstance(records, list) or len(records) > _BATCH_SIZE:
            raise FeishuBaseError("Projection operation has invalid batch size.", operation="projection write")
        if kind == "create":
            created = self.client.create_records(app, table, [{"fields": item["fields"]} for item in records])
            if len(created) != len(records):
                raise FeishuBaseError("Feishu returned an incomplete create batch.", operation="projection create")
            for source, target in zip(records, created, strict=True):
                record_id = target.get("record_id")
                if not isinstance(record_id, str) or not record_id:
                    raise FeishuBaseError("Feishu create response omitted record ID.", operation="projection create")
                self.store.set_projection_record_mapping(f"record:{table_key}", str(source["business_key"]), record_id)
        elif kind == "update":
            current = mappings or self.store.list_projection_record_mappings(f"record:{table_key}")
            payload = []
            for item in records:
                record_id = current.get(str(item["business_key"]))
                if not record_id:
                    raise FeishuBaseError("Projection record mapping is missing.", operation="projection update")
                payload.append({"record_id": record_id, "fields": item["fields"]})
            self.client.update_records(app, table, payload)
        else:
            raise FeishuBaseError("Projection operation is unknown.", operation="projection write")

    def _drain_outbox(self, app: str, tables: dict[str, str]) -> bool:
        all_ok = True
        for item in self.store.list_projection_outbox():
            try:
                payload = json.loads(str(item["payload"]))
                if not isinstance(payload, dict) or payload.get("table_key") not in tables:
                    raise FeishuBaseError("Projection outbox payload is invalid.", operation="projection outbox")
                self._execute(payload)
                self.store.complete_projection_outbox(int(item["id"]))
            except (json.JSONDecodeError, FeishuBaseError) as exc:
                self.store.mark_projection_outbox_attempt(int(item["id"]), getattr(exc, "operation", "projection outbox"))
                all_ok = False
        return all_ok
