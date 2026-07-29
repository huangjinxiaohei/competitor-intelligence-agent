from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .models import Candidate, ChangeEvent, ProductSnapshot, RunResult


class StateStore:
    """SQLite-backed persistence for candidates, snapshots, runs, and diffs."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> StateStore:
        self.initialize()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

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
            """
        )
        self.connection.commit()

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
                age_seconds = (now - acquired_at).total_seconds()
                if age_seconds <= lock_ttl_seconds:
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
