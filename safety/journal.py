from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Lifecycle, TERMINAL_STATES


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JournalError(RuntimeError):
    pass


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def _initialize(self) -> None:
        with self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS system_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    kill_latched INTEGER NOT NULL,
                    kill_reason TEXT,
                    generation INTEGER NOT NULL,
                    reconciliation_complete INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    target_service TEXT NOT NULL,
                    state TEXT NOT NULL,
                    outcome TEXT,
                    cleanup_status TEXT,
                    recovery_status TEXT,
                    policy_version TEXT NOT NULL,
                    baseline_json TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS faults (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(id),
                    generation INTEGER NOT NULL,
                    target_service TEXT NOT NULL,
                    container_id TEXT NOT NULL,
                    fault_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    desired_cancel INTEGER NOT NULL,
                    rollback_intent INTEGER NOT NULL,
                    original_state_json TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    lease_expires_utc TEXT,
                    maximum_expires_utc TEXT NOT NULL,
                    ownership_token TEXT NOT NULL,
                    cleanup_verified INTEGER NOT NULL,
                    residual_evidence TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id TEXT,
                    fault_id TEXT,
                    timestamp TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason_code TEXT,
                    target TEXT,
                    details_json TEXT NOT NULL,
                    correlation_id TEXT NOT NULL
                );
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO system_state
                (singleton, kill_latched, kill_reason, generation, reconciliation_complete, updated_at)
                VALUES (1, 0, NULL, 1, 1, ?)
                """,
                (utc_now(),),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def system_state(self) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute("SELECT * FROM system_state WHERE singleton=1").fetchone()
        return dict(row)

    def set_reconciliation(self, complete: bool) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE system_state SET reconciliation_complete=?, updated_at=? WHERE singleton=1",
                (int(complete), utc_now()),
            )

    def latch_kill(self, reason: str) -> int:
        with self.transaction() as db:
            row = db.execute("SELECT generation FROM system_state WHERE singleton=1").fetchone()
            generation = int(row[0]) + 1
            db.execute(
                """UPDATE system_state SET kill_latched=1, kill_reason=?, generation=?,
                   updated_at=? WHERE singleton=1""",
                (reason, generation, utc_now()),
            )
            db.execute(
                "UPDATE faults SET desired_cancel=1, updated_at=? WHERE cleanup_verified=0",
                (utc_now(),),
            )
        return generation

    def reset_kill(self) -> int:
        with self.transaction() as db:
            unfinished = db.execute(
                "SELECT COUNT(*) FROM faults WHERE cleanup_verified=0"
            ).fetchone()[0]
            if unfinished:
                raise JournalError("Cannot reset while cleanup is unfinished")
            state = db.execute("SELECT reconciliation_complete, generation FROM system_state").fetchone()
            if not state[0]:
                raise JournalError("Cannot reset before reconciliation completes")
            generation = int(state[1]) + 1
            db.execute(
                """UPDATE system_state SET kill_latched=0, kill_reason=NULL, generation=?,
                   updated_at=? WHERE singleton=1""",
                (generation, utc_now()),
            )
        return generation

    def finalize_orphans(self) -> None:
        """Close interrupted controllers only after every recorded fault is removed."""
        with self.transaction() as db:
            if db.execute("SELECT COUNT(*) FROM faults WHERE cleanup_verified=0").fetchone()[0]:
                raise JournalError("Cannot close interrupted experiments with residual faults")
            placeholders = ",".join("?" for _ in TERMINAL_STATES)
            now = utc_now()
            db.execute(
                f"""UPDATE experiments SET state='ABORTED', outcome='aborted',
                    cleanup_status='verified', recovery_status='not_confirmed',
                    updated_at=?, finished_at=? WHERE state NOT IN ({placeholders})""",
                (now, now, *TERMINAL_STATES),
            )

    def begin_experiment(
        self, experiment_id: str, profile: str, target: str, policy_version: str
    ) -> int:
        with self.transaction() as db:
            state = db.execute("SELECT * FROM system_state WHERE singleton=1").fetchone()
            if state["kill_latched"] or not state["reconciliation_complete"]:
                raise JournalError("Experiment admission is latched off")
            placeholders = ",".join("?" for _ in TERMINAL_STATES)
            active = db.execute(
                f"SELECT id FROM experiments WHERE state NOT IN ({placeholders}) LIMIT 1",
                tuple(TERMINAL_STATES),
            ).fetchone()
            if active:
                raise JournalError(f"Experiment already active: {active['id']}")
            now = utc_now()
            db.execute(
                """INSERT INTO experiments
                   (id, profile, target_service, state, policy_version, started_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (experiment_id, profile, target, Lifecycle.PREFLIGHT.value, policy_version, now, now),
            )
            return int(state["generation"])

    def transition(
        self,
        experiment_id: str,
        state: str,
        *,
        outcome: str | None = None,
        cleanup_status: str | None = None,
        recovery_status: str | None = None,
        baseline: dict[str, Any] | None = None,
    ) -> None:
        finished_at = utc_now() if state in TERMINAL_STATES else None
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE experiments SET state=?, outcome=COALESCE(?, outcome),
                   cleanup_status=COALESCE(?, cleanup_status),
                   recovery_status=COALESCE(?, recovery_status),
                   baseline_json=COALESCE(?, baseline_json), updated_at=?,
                   finished_at=COALESCE(?, finished_at) WHERE id=?""",
                (
                    state,
                    outcome,
                    cleanup_status,
                    recovery_status,
                    json.dumps(baseline) if baseline is not None else None,
                    utc_now(),
                    finished_at,
                    experiment_id,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalError(f"Unknown experiment: {experiment_id}")

    def create_fault(self, values: dict[str, Any]) -> str:
        fault_id = values.get("id") or str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO faults
                   (id, experiment_id, generation, target_service, container_id,
                    fault_type, state, desired_cancel, rollback_intent,
                    original_state_json, spec_json, lease_expires_utc,
                    maximum_expires_utc, ownership_token, cleanup_verified,
                    residual_evidence, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, 0, NULL, ?, ?)""",
                (
                    fault_id,
                    values["experiment_id"],
                    values["generation"],
                    values["target_service"],
                    values["container_id"],
                    values["fault_type"],
                    values.get("state", "PREPARED"),
                    json.dumps(values["original_state"]),
                    json.dumps(values["spec"]),
                    values.get("lease_expires_utc"),
                    values["maximum_expires_utc"],
                    values["ownership_token"],
                    now,
                    now,
                ),
            )
        return fault_id

    def fault(self, fault_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()
        return dict(row) if row else None

    def unfinished_faults(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM faults WHERE cleanup_verified=0 ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_fault(self, fault_id: str, **updates: Any) -> None:
        allowed = {
            "state",
            "desired_cancel",
            "lease_expires_utc",
            "cleanup_verified",
            "residual_evidence",
        }
        invalid = set(updates) - allowed
        if invalid:
            raise JournalError(f"Invalid fault fields: {invalid}")
        updates["updated_at"] = utc_now()
        columns = ", ".join(f"{column}=?" for column in updates)
        with self.transaction() as db:
            cursor = db.execute(
                f"UPDATE faults SET {columns} WHERE id=?",
                (*updates.values(), fault_id),
            )
            if cursor.rowcount != 1:
                raise JournalError(f"Unknown fault: {fault_id}")

    def add_event(
        self,
        *,
        state: str,
        reason_code: str | None,
        details: dict[str, Any],
        experiment_id: str | None = None,
        fault_id: str | None = None,
        target: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO events
                   (experiment_id, fault_id, timestamp, state, reason_code, target,
                    details_json, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    experiment_id,
                    fault_id,
                    utc_now(),
                    state,
                    reason_code,
                    target,
                    json.dumps(details, sort_keys=True),
                    correlation_id or str(uuid.uuid4()),
                ),
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            experiments = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM experiments ORDER BY started_at DESC LIMIT 10"
                ).fetchall()
            ]
            faults = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM faults ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
            ]
            events = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM events ORDER BY sequence DESC LIMIT 30"
                ).fetchall()
            ]
        return {
            "system": self.system_state(),
            "experiments": experiments,
            "faults": faults,
            "events": events,
        }

