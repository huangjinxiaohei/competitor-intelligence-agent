from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .discovery import site_origin, stable_candidate_id
from .models import Candidate, ChangeEvent, ProductSnapshot, RunResult


class StateStore:
    """SQLite-backed persistence for local intelligence state and projections."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.connection: sqlite3.Connection | None = None
        self._migration_checked = False

    def __enter__(self) -> StateStore:
        self.initialize()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
            self._migration_checked = False

    def initialize(self) -> None:
        if self.connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.db_path)
            self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_candidate_observed
                ON snapshots(candidate_id, observed_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                status TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                payload TEXT
            );
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                field_path TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS missing_counts (
                candidate_id TEXT NOT NULL,
                field_path TEXT NOT NULL,
                missing_count INTEGER NOT NULL,
                PRIMARY KEY(candidate_id, field_path)
            );
            CREATE TABLE IF NOT EXISTS run_locks (
                project_id TEXT PRIMARY KEY,
                acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS candidate_aliases (
                alias_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_aliases_candidate
                ON candidate_aliases(candidate_id);
            CREATE TABLE IF NOT EXISTS projection_resources (
                resource_key TEXT PRIMARY KEY,
                resource_id TEXT NOT NULL,
                link TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS projection_record_mappings (
                resource_key TEXT NOT NULL,
                business_key TEXT NOT NULL,
                record_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(resource_key, business_key)
            );
            CREATE TABLE IF NOT EXISTS projection_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_projection_outbox_pending
                ON projection_outbox(completed_at, id);
            """
        )
        self.connection.commit()
        if not self._migration_checked:
            self.migrate_candidate_identities()
            self._migration_checked = True

    @property
    def _db(self) -> sqlite3.Connection:
        self.initialize()
        assert self.connection is not None
        return self.connection

    def acquire_run_lock(self, project_id: str, lock_ttl_seconds: float = 6 * 60 * 60) -> bool:
        """Acquire a project lock, reclaiming only locks older than the configured TTL."""
        database = self._db
        now = datetime.now(UTC)
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT acquired_at FROM run_locks WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is not None:
                acquired_at = datetime.fromisoformat(row["acquired_at"])
                if acquired_at.tzinfo is None:
                    acquired_at = acquired_at.replace(tzinfo=UTC)
                if (now - acquired_at).total_seconds() <= lock_ttl_seconds:
                    database.rollback()
                    return False
                database.execute("DELETE FROM run_locks WHERE project_id = ?", (project_id,))
            database.execute(
                "INSERT INTO run_locks(project_id, acquired_at) VALUES (?, ?)",
                (project_id, now.isoformat()),
            )
            database.commit()
            return True
        except Exception:
            database.rollback()
            raise

    def release_run_lock(self, project_id: str) -> None:
        self._db.execute("DELETE FROM run_locks WHERE project_id = ?", (project_id,))
        self._db.commit()

    def save_candidates(self, candidates: Iterable[Candidate]) -> None:
        self._db.executemany(
            """INSERT INTO candidates(candidate_id, payload) VALUES (?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET payload = excluded.payload,
            updated_at = CURRENT_TIMESTAMP""",
            [(item.id, item.model_dump_json()) for item in candidates],
        )
        self._db.commit()

    def start_run(self, run_id: str, started_at: datetime) -> None:
        self._db.execute(
            """INSERT INTO runs(run_id, status, started_at) VALUES (?, 'running', ?)
            ON CONFLICT(run_id) DO UPDATE SET status = 'running', started_at = excluded.started_at,
            finished_at = NULL, payload = NULL""",
            (run_id, started_at.isoformat()),
        )
        self._db.commit()

    def finish_run(self, result: RunResult) -> None:
        self._db.execute(
            """INSERT INTO runs(run_id, status, started_at, finished_at, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET status = excluded.status,
            started_at = excluded.started_at, finished_at = excluded.finished_at,
            payload = excluded.payload""",
            (result.run_id, result.status.value, result.started_at.isoformat(),
             result.finished_at.isoformat(), result.model_dump_json()),
        )
        self._db.commit()

    def get_latest_snapshot(self, candidate_id: str) -> ProductSnapshot | None:
        row = self._db.execute(
            """SELECT payload FROM snapshots WHERE candidate_id = ?
            ORDER BY observed_at DESC, id DESC LIMIT 1""", (candidate_id,)
        ).fetchone()
        return ProductSnapshot.model_validate_json(row["payload"]) if row else None

    def save_snapshot(self, snapshot: ProductSnapshot) -> None:
        self._db.execute(
            "INSERT INTO snapshots(candidate_id, observed_at, payload) VALUES (?, ?, ?)",
            (snapshot.candidate_id, snapshot.observed_at.isoformat(), snapshot.model_dump_json()),
        )
        self._db.commit()

    def get_missing_count(self, candidate_id: str, field_path: str) -> int:
        row = self._db.execute(
            "SELECT missing_count FROM missing_counts WHERE candidate_id = ? AND field_path = ?",
            (candidate_id, field_path),
        ).fetchone()
        return int(row["missing_count"]) if row else 0

    def set_missing_count(self, candidate_id: str, field_path: str, count: int) -> None:
        self._db.execute(
            """INSERT INTO missing_counts(candidate_id, field_path, missing_count) VALUES (?, ?, ?)
            ON CONFLICT(candidate_id, field_path) DO UPDATE SET missing_count = excluded.missing_count""",
            (candidate_id, field_path, count),
        )
        self._db.commit()

    def save_changes(self, run_id: str, events: Iterable[ChangeEvent]) -> None:
        self._db.executemany(
            "INSERT INTO changes(run_id, candidate_id, field_path, payload) VALUES (?, ?, ?, ?)",
            [(run_id, event.candidate_id, event.field_path, event.model_dump_json()) for event in events],
        )
        self._db.commit()

    def list_runs(self) -> list[dict[str, object]]:
        rows = self._db.execute(
            "SELECT run_id, status, started_at, finished_at, payload FROM runs ORDER BY started_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_candidate_alias(self, candidate_id: str) -> str:
        row = self._db.execute(
            "SELECT candidate_id FROM candidate_aliases WHERE alias_id = ?", (candidate_id,)
        ).fetchone()
        return str(row["candidate_id"]) if row else candidate_id

    def migrate_candidate_identities(self) -> None:
        """Merge legacy URL/name identities into stable registrable-domain IDs.

        All rewrites are in one transaction, preserve every child row, and record
        every replaced ID as an alias. Re-running it makes no additional changes.
        """
        if self.connection is None:
            self.initialize()
        assert self.connection is not None
        database = self.connection
        rows = database.execute("SELECT candidate_id, payload FROM candidates").fetchall()
        if not rows:
            return
        groups: dict[str, list[tuple[str, Candidate]]] = {}
        for row in rows:
            candidate = Candidate.model_validate_json(row["payload"])
            target_id = stable_candidate_id(candidate.homepage)
            groups.setdefault(target_id, []).append((str(row["candidate_id"]), candidate))
        rewrites = {old_id: target_id for target_id, members in groups.items() for old_id, _ in members if old_id != target_id}
        if not rewrites:
            return
        try:
            database.execute("BEGIN IMMEDIATE")
            for old_id, target_id in rewrites.items():
                database.execute(
                    """INSERT INTO candidate_aliases(alias_id, candidate_id) VALUES (?, ?)
                    ON CONFLICT(alias_id) DO UPDATE SET candidate_id = excluded.candidate_id""",
                    (old_id, target_id),
                )
            for old_id, target_id in rewrites.items():
                self._rewrite_child_candidate_id("snapshots", old_id, target_id)
                self._rewrite_child_candidate_id("changes", old_id, target_id)
                self._rewrite_missing_counts(old_id, target_id)
            for target_id, members in groups.items():
                ordered = sorted(members, key=lambda item: (item[1].name.casefold(), item[0]))
                _, selected = ordered[0]
                entries = sorted({entry for _, candidate in members for entry in (candidate.official_entry_urls or [candidate.homepage])})
                merged = selected.model_copy(update={
                    "id": target_id,
                    "homepage": site_origin(selected.homepage),
                    "official_entry_urls": entries,
                })
                for old_id, _ in members:
                    database.execute("DELETE FROM candidates WHERE candidate_id = ?", (old_id,))
                database.execute(
                    "INSERT INTO candidates(candidate_id, payload) VALUES (?, ?)",
                    (target_id, merged.model_dump_json()),
                )
            database.commit()
        except Exception:
            database.rollback()
            raise

    def _rewrite_missing_counts(self, old_id: str, target_id: str) -> None:
        assert self.connection is not None
        database = self.connection
        rows = database.execute(
            "SELECT field_path, missing_count FROM missing_counts WHERE candidate_id = ?", (old_id,)
        ).fetchall()
        for row in rows:
            database.execute(
                """INSERT INTO missing_counts(candidate_id, field_path, missing_count) VALUES (?, ?, ?)
                ON CONFLICT(candidate_id, field_path) DO UPDATE SET
                missing_count = MAX(missing_counts.missing_count, excluded.missing_count)""",
                (target_id, row["field_path"], row["missing_count"]),
            )
        database.execute("DELETE FROM missing_counts WHERE candidate_id = ?", (old_id,))

    def _rewrite_child_candidate_id(self, table: str, old_id: str, target_id: str) -> None:
        assert self.connection is not None
        database = self.connection
        rows = database.execute(f"SELECT id, payload FROM {table} WHERE candidate_id = ?", (old_id,)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            payload["candidate_id"] = target_id
            database.execute(
                f"UPDATE {table} SET candidate_id = ?, payload = ? WHERE id = ?",
                (target_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["id"]),
            )

    def set_projection_resource(self, resource_key: str, resource_id: str, link: str | None = None) -> None:
        self._db.execute(
            """INSERT INTO projection_resources(resource_key, resource_id, link) VALUES (?, ?, ?)
            ON CONFLICT(resource_key) DO UPDATE SET resource_id = excluded.resource_id,
            link = excluded.link, updated_at = CURRENT_TIMESTAMP""",
            (resource_key, resource_id, link),
        )
        self._db.commit()

    def get_projection_resource(self, resource_key: str) -> str | None:
        row = self._db.execute("SELECT resource_id FROM projection_resources WHERE resource_key = ?", (resource_key,)).fetchone()
        return str(row["resource_id"]) if row else None

    def get_projection_resource_link(self, resource_key: str) -> str | None:
        row = self._db.execute("SELECT link FROM projection_resources WHERE resource_key = ?", (resource_key,)).fetchone()
        return str(row["link"]) if row and row["link"] is not None else None

    def set_projection_record_mapping(self, resource_key: str, business_key: str, record_id: str) -> None:
        self._db.execute(
            """INSERT INTO projection_record_mappings(resource_key, business_key, record_id) VALUES (?, ?, ?)
            ON CONFLICT(resource_key, business_key) DO UPDATE SET record_id = excluded.record_id,
            updated_at = CURRENT_TIMESTAMP""",
            (resource_key, business_key, record_id),
        )
        self._db.commit()

    def get_projection_record_mapping(self, resource_key: str, business_key: str) -> str | None:
        row = self._db.execute(
            "SELECT record_id FROM projection_record_mappings WHERE resource_key = ? AND business_key = ?",
            (resource_key, business_key),
        ).fetchone()
        return str(row["record_id"]) if row else None

    def enqueue_projection_outbox(self, operation: str, payload: dict[str, Any]) -> int:
        cursor = self._db.execute(
            "INSERT INTO projection_outbox(operation, payload) VALUES (?, ?)",
            (operation, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )
        self._db.commit()
        return int(cursor.lastrowid)

    def list_projection_outbox(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, operation, payload, attempts, last_error, created_at FROM projection_outbox WHERE completed_at IS NULL ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_projection_outbox_attempt(self, outbox_id: int, error: str = "") -> None:
        self._db.execute(
            "UPDATE projection_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ? AND completed_at IS NULL",
            (error, outbox_id),
        )
        self._db.commit()

    def complete_projection_outbox(self, outbox_id: int) -> None:
        self._db.execute(
            "UPDATE projection_outbox SET completed_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), outbox_id)
        )
        self._db.commit()
