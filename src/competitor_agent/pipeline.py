from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .adapters.search import SearchResult, StaticSearchProvider
from .analyzer import HeuristicAnalyzer, HttpJsonAnalyzer
from .collector import collect_candidate
from .config import ProjectConfig, load_config
from .delivery import (
    DeliveryAdapter,
    MockDeliveryAdapter,
    WebhookDeliveryAdapter,
)
from .diffing import diff_snapshots
from .discovery import discover_candidates, score_candidate
from .models import (
    Candidate,
    ChangeEvent,
    DeliveryReceipt,
    ProductSnapshot,
    RunResult,
    RunStatus,
)
from .reporting import build_digest, write_reports
from .storage import StateStore


DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures"


def _fixture_candidates(config: ProjectConfig) -> list[Candidate]:
    return [
        score_candidate(
            name=name,
            homepage=f"fixture://{stem}",
            category=config.project.topic,
            signals={key: 1.0 for key in config.discovery.weights},
            config=config,
        )
        for name, stem in (("NovaBoard", "novaboard"), ("OrbitNote", "orbitnote"))
    ]


def _host_results(config: ProjectConfig) -> StaticSearchProvider:
    """Load host-agent search handoff JSON, falling back to configured seed URLs."""
    handoff = Path(config.storage.database).parent / "host_search_results.json"
    results_by_query: dict[str, list[SearchResult]] = {}
    if handoff.is_file():
        raw = json.loads(handoff.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            parsed = [SearchResult(**item) for item in raw]
            for query in [*config.project.keywords, config.project.topic]:
                results_by_query[query] = parsed
        elif isinstance(raw, dict):
            results_by_query = {
                str(query): [SearchResult(**item) for item in items]
                for query, items in raw.items()
            }
    if not results_by_query and config.project.seed_urls:
        seeds = [
            SearchResult(title=url, url=url, snippet=config.project.topic)
            for url in config.project.seed_urls
        ]
        for query in [*config.project.keywords, config.project.topic]:
            results_by_query[query] = seeds
    return StaticSearchProvider(results_by_query)


def discover_only(
    config_path: str | Path = "config/project.yaml",
    fixture: bool = False,
    dry_run: bool = False,
) -> list[Candidate]:
    del dry_run
    load_dotenv()
    config = load_config(config_path)
    if fixture:
        return _fixture_candidates(config)
    return discover_candidates(config, _host_results(config))


def _fixture_round(
    store: StateStore,
    fixture_root: Path,
) -> Path:
    completed_runs = [
        item for item in store.list_runs() if item.get("status") != "running"
    ]
    round_number = min(len(completed_runs) + 1, 3)
    matches = sorted(fixture_root.glob(f"round_{round_number:02d}_*"))
    if not matches:
        raise FileNotFoundError(f"Fixture round {round_number} is missing")
    return matches[0]


def _tracked_paths(snapshot: ProductSnapshot) -> set[str]:
    paths = {"availability"}

    def flatten(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                flatten(f"{prefix}.{key}", item)
        else:
            paths.add(prefix)

    flatten("specifications", snapshot.specifications)
    flatten("configurations", snapshot.configurations)
    for tier in snapshot.pricing:
        for key in type(tier).model_fields:
            if key != "name":
                paths.add(f"pricing.{tier.name.casefold()}.{key}")
    return paths


def _diff_with_persistent_missing_counts(
    store: StateStore,
    previous: ProductSnapshot | None,
    current: ProductSnapshot,
) -> list[ChangeEvent]:
    if previous is None:
        return []
    paths = _tracked_paths(previous) | _tracked_paths(current)
    counts = {
        path: store.get_missing_count(current.candidate_id, path) for path in paths
    }
    events = diff_snapshots(previous, current, counts)
    for path, count in counts.items():
        store.set_missing_count(current.candidate_id, path, count)
    return events


def _snapshot_for_persistence(
    previous: ProductSnapshot | None,
    current: ProductSnapshot,
    events: Iterable[ChangeEvent],
) -> ProductSnapshot:
    """Keep a missing fact in the baseline until its deletion is confirmed."""
    if previous is None:
        return current
    pending = [event for event in events if not event.confirmed and event.after is None]
    if not pending:
        return current
    payload = current.model_dump(mode="python")

    def set_nested(root: dict[str, Any], parts: list[str], value: Any) -> None:
        node = root
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    for event in pending:
        parts = event.field_path.split(".")
        if parts == ["availability"]:
            payload["availability"] = event.before
        elif parts[0] in {"specifications", "configurations"}:
            set_nested(payload[parts[0]], parts[1:], event.before)
        elif parts[0] == "pricing" and len(parts) >= 3:
            tier_key, field = parts[1].casefold(), parts[2]
            tier_payload = next(
                (item for item in payload["pricing"] if str(item["name"]).casefold() == tier_key),
                None,
            )
            if tier_payload is None:
                previous_tier = next(
                    (item for item in previous.pricing if item.name.casefold() == tier_key),
                    None,
                )
                if previous_tier is not None:
                    tier_payload = previous_tier.model_dump(mode="python")
                    payload["pricing"].append(tier_payload)
            if tier_payload is not None:
                tier_payload[field] = event.before

    known = {
        (item.source_url, item.excerpt, item.observed_at)
        for item in current.evidence
    }
    payload["evidence"] = [item.model_dump(mode="python") for item in current.evidence]
    payload["evidence"].extend(
        item.model_dump(mode="python")
        for item in previous.evidence
        if (item.source_url, item.excerpt, item.observed_at) not in known
    )
    return ProductSnapshot.model_validate(payload)


def _delivery(
    config: ProjectConfig,
    adapter: str | DeliveryAdapter | None,
) -> DeliveryAdapter:
    if adapter is not None and not isinstance(adapter, str):
        return adapter
    name = adapter or os.getenv("DELIVERY_ADAPTER") or config.adapters.delivery
    if name == "webhook":
        return WebhookDeliveryAdapter()
    return MockDeliveryAdapter()


def run_pipeline(
    config_path: str | Path = "config/project.yaml",
    fixture: bool = False,
    dry_run: bool = False,
    delivery_adapter: str | DeliveryAdapter | None = None,
    force_publish: bool = False,
    fixture_root: str | Path = DEFAULT_FIXTURE_ROOT,
) -> RunResult:
    load_dotenv()
    config = load_config(config_path)
    started_at = datetime.now(UTC)
    run_id = f"{started_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    if dry_run and not fixture:
        candidates = discover_only(config_path, fixture=False, dry_run=True)
        return RunResult(
            run_id=run_id,
            status=RunStatus.SUCCESS,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            candidate_count=len(candidates),
            snapshot_count=0,
            change_count=0,
            errors=[],
        )
    analyzer = (
        HttpJsonAnalyzer()
        if config.adapters.analyzer == "http-json"
        else HeuristicAnalyzer()
    )
    store = StateStore(config.storage.database)
    store.initialize()
    if not store.acquire_run_lock(config.project.id):
        store.close()
        raise RuntimeError(f"Project {config.project.id!r} already has an active run")

    try:
        first_run = not [
            item for item in store.list_runs() if item.get("status") != "running"
        ]
        fixture_dir = (
            _fixture_round(store, Path(fixture_root)) if fixture else None
        )
        store.start_run(run_id, started_at)
        candidates = (
            _fixture_candidates(config)
            if fixture
            else discover_candidates(config, _host_results(config))
        )
        store.save_candidates(candidates)

        snapshots: list[ProductSnapshot] = []
        all_events: list[ChangeEvent] = []
        errors: list[str] = []
        for candidate in candidates:
            if candidate.status.value != "monitored":
                continue
            try:
                documents = collect_candidate(
                    candidate,
                    config,
                    fixture_dir=fixture_dir,
                )
                if not documents:
                    raise ValueError("no official source documents were collected")
                snapshot = analyzer.analyze(candidate, documents)
                if not snapshot.evidence:
                    raise ValueError("no evidence-backed structured facts were extracted")
                previous = store.get_latest_snapshot(candidate.id)
                events = _diff_with_persistent_missing_counts(
                    store, previous, snapshot
                )
                persisted_snapshot = _snapshot_for_persistence(
                    previous, snapshot, events
                )
                store.save_snapshot(persisted_snapshot)
                snapshots.append(snapshot)
                all_events.extend(events)
            except Exception as exc:
                errors.append(f"{candidate.name}: {type(exc).__name__}: {exc}")

        store.save_changes(run_id, all_events)
        confirmed_events = [event for event in all_events if event.confirmed]
        report_directory = (
            Path(config.storage.reports_dir) / started_at.date().isoformat()
        )
        report_path = report_directory / f"{run_id}.md"
        digest = build_digest(
            run_id,
            candidates,
            snapshots,
            confirmed_events,
            errors,
            report_path,
            first_run,
        )
        write_reports(
            digest,
            candidates,
            snapshots,
            all_events,
            errors,
            report_directory,
        )

        receipt: DeliveryReceipt | None = None
        should_publish = force_publish or first_run or bool(confirmed_events)
        if should_publish and not dry_run:
            receipt = _delivery(config, delivery_adapter).publish(digest)
            if not receipt.delivered:
                errors.append(f"Delivery: {receipt.detail}")

        status = RunStatus.PARTIAL if errors else RunStatus.SUCCESS
        result = RunResult(
            run_id=run_id,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            candidate_count=len(candidates),
            snapshot_count=len(snapshots),
            change_count=len(confirmed_events),
            digest=digest,
            delivery=receipt,
            errors=errors,
        )
        store.finish_run(result)
        return result
    finally:
        store.release_run_lock(config.project.id)
        store.close()


def report_run(
    config_path: str | Path,
    run_id: str,
    fixture: bool = False,
    dry_run: bool = False,
) -> str:
    del fixture, dry_run
    config = load_config(config_path)
    matches = sorted(Path(config.storage.reports_dir).rglob(f"{run_id}.md"))
    if not matches:
        raise FileNotFoundError(f"Report for run {run_id!r} was not found")
    return str(matches[-1])
