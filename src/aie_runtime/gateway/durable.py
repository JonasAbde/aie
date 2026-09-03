from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aie_runtime.errors import AIEError


class SQLiteGatewayStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5.0)
        con.row_factory = sqlite3.Row
        return con

    def _initialize(self) -> None:
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS outcomes (
                    action_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revocations (
                    lease_id TEXT PRIMARY KEY,
                    revoked_at TEXT NOT NULL,
                    source_gateway TEXT
                );
                CREATE TABLE IF NOT EXISTS lease_budget (
                    lease_id TEXT PRIMARY KEY,
                    remaining REAL NOT NULL CHECK (remaining >= 0)
                );
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    action_id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK (amount >= 0),
                    state TEXT NOT NULL CHECK (state IN ('reserved','committed','rolled_back')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(revocations)").fetchall()}
            if "source_gateway" not in columns:
                con.execute("ALTER TABLE revocations ADD COLUMN source_gateway TEXT")
            outcome_columns = {row[1] for row in con.execute("PRAGMA table_info(outcomes)").fetchall()}
            if "fingerprint" not in outcome_columns:
                # ponytail: dedupe discriminator for id-reuse with different content;
                # NULL = legacy row, treated conservatively as a match.
                con.execute("ALTER TABLE outcomes ADD COLUMN fingerprint TEXT")

    def put_outcome(
        self,
        action_id: str,
        *,
        status: str,
        protocol: str,
        error_code: str | None,
        fingerprint: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO outcomes(action_id,status,protocol,error_code,fingerprint,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(action_id) DO UPDATE SET
                    status=excluded.status,
                    protocol=excluded.protocol,
                    error_code=excluded.error_code,
                    fingerprint=excluded.fingerprint,
                    created_at=excluded.created_at
                """,
                (action_id, status, protocol, error_code, fingerprint, now),
            )

    def get_outcome(self, action_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT action_id,status,protocol,error_code,fingerprint FROM outcomes WHERE action_id=?",
                (action_id,),
            ).fetchone()
        return dict(row) if row else None

    def clear_reservation(self, action_id: str) -> bool:
        """Retire the budget marker for an action_id being reprocessed with new
        content. Money-neutral in every state: committed/rolled_back already
        settled, reserved is refunded first. Returns True if a marker existed."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT lease_id,amount,state FROM budget_reservations WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                con.commit()
                return False
            if row["state"] == "reserved":
                con.execute(
                    "UPDATE lease_budget SET remaining=remaining+? WHERE lease_id=?",
                    (float(row["amount"]), row["lease_id"]),
                )
            con.execute("DELETE FROM budget_reservations WHERE action_id=?", (action_id,))
            con.commit()
            return True
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def revoke(
        self,
        lease_id: str,
        *,
        revoked_at: str | None = None,
        source_gateway: str | None = None,
    ) -> None:
        when = revoked_at or datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO revocations(lease_id,revoked_at,source_gateway) VALUES(?,?,?)",
                (lease_id, when, source_gateway),
            )

    def is_revoked(self, lease_id: str) -> bool:
        with self._connect() as con:
            row = con.execute("SELECT 1 FROM revocations WHERE lease_id=?", (lease_id,)).fetchone()
        return row is not None

    def initialize_budget(self, lease_id: str, amount: float) -> None:
        if amount < 0:
            raise AIEError("AIE-BUDGET-001")
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO lease_budget(lease_id,remaining) VALUES(?,?)",
                (lease_id, float(amount)),
            )

    def remaining_budget(self, lease_id: str) -> float:
        with self._connect() as con:
            row = con.execute("SELECT remaining FROM lease_budget WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise AIEError("AIE-BUDGET-001")
        return float(row["remaining"])

    def reserve_budget(self, lease_id: str, action_id: str, amount: float) -> None:
        if amount < 0:
            raise AIEError("AIE-BUDGET-001")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT state FROM budget_reservations WHERE action_id=?", (action_id,)
            ).fetchone()
            if existing is not None:
                raise AIEError("AIE-REPLAY-001")
            row = con.execute("SELECT remaining FROM lease_budget WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None or float(row["remaining"]) < amount:
                raise AIEError("AIE-BUDGET-001")
            con.execute(
                "UPDATE lease_budget SET remaining=remaining-? WHERE lease_id=?",
                (float(amount), lease_id),
            )
            con.execute(
                "INSERT INTO budget_reservations(action_id,lease_id,amount,state,created_at) VALUES(?,?,?,?,?)",
                (action_id, lease_id, float(amount), "reserved", datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def commit_budget(self, action_id: str) -> None:
        with self._connect() as con:
            row = con.execute(
                "SELECT state FROM budget_reservations WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise AIEError("AIE-BUDGET-001")
            if row["state"] == "reserved":
                con.execute(
                    "UPDATE budget_reservations SET state='committed' WHERE action_id=?",
                    (action_id,),
                )

    def rollback_budget(self, action_id: str) -> None:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT lease_id,amount,state FROM budget_reservations WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise AIEError("AIE-BUDGET-001")
            if row["state"] == "reserved":
                con.execute(
                    "UPDATE lease_budget SET remaining=remaining+? WHERE lease_id=?",
                    (float(row["amount"]), row["lease_id"]),
                )
                con.execute(
                    "UPDATE budget_reservations SET state='rolled_back' WHERE action_id=?",
                    (action_id,),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def reservation_state(self, action_id: str) -> str | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT state FROM budget_reservations WHERE action_id=?", (action_id,)
            ).fetchone()
        return str(row["state"]) if row else None

    def append_evidence(self, event: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "INSERT INTO evidence(event_json,created_at) VALUES(?,?)",
                (json.dumps(event, sort_keys=True, separators=(",", ":")), now),
            )

    def list_evidence(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT event_json FROM evidence ORDER BY seq ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]
