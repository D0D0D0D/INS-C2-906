"""SQLite implementation of the SDK ``BaseDatabase`` persistence port."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.services.database.base_db import BaseDatabase


_POSTGRES_LOOKBACK = re.compile(
    r"created_at\s*>=\s*NOW\(\)\s*-\s*make_interval\(days\s*=>\s*:lookback_days\)",
    re.IGNORECASE,
)
_JSON_COLUMNS = frozenset({"consistency_flags"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assessment_cases (
    case_id                   TEXT NOT NULL,
    revision                  INTEGER NOT NULL,
    policy_type               TEXT NOT NULL DEFAULT '',
    coverage_item             TEXT NOT NULL DEFAULT '',
    incident_type             TEXT NOT NULL DEFAULT '',
    injury_category           TEXT NOT NULL DEFAULT '',
    treatment_or_repair_period TEXT,
    claimed_amount            REAL,
    consistency_flags         TEXT NOT NULL DEFAULT '[]',
    anomaly_score             REAL,
    final_verdict             TEXT NOT NULL DEFAULT 'needs_review',
    assessor_edited           INTEGER NOT NULL DEFAULT 0,
    claimant_key              TEXT NOT NULL DEFAULT '',
    source_row_hash           TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    PRIMARY KEY (case_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_assessment_cases_population
    ON assessment_cases (policy_type, coverage_item, final_verdict);
CREATE INDEX IF NOT EXISTS idx_assessment_cases_claimant_created
    ON assessment_cases (claimant_key, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_cases_source_row_hash
    ON assessment_cases (source_row_hash)
    WHERE source_row_hash IS NOT NULL AND source_row_hash <> '';
"""


class SQLiteDatabase(BaseDatabase):
    """SQLite adapter for ``fetch_one`` / ``fetch_many`` / ``execute``.

    A shared-cache keeper connection makes ``:memory:`` obey the same
    multi-connection behavior as a file database. Every public operation uses
    its own connection, so reads and writes created by the repository factory
    always address the same configured store and are safe across request
    threads.
    """

    def __init__(self, path: str):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("SQLite database path must be a non-empty string")

        configured_path = path.strip()
        self._keeper: sqlite3.Connection | None = None
        self._close_lock = threading.Lock()
        if configured_path == ":memory:":
            self._database = f"file:ins_c2_906_{uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._open_connection()
            schema_connection = self._keeper
        else:
            database_path = Path(configured_path).expanduser()
            if not database_path.parent.exists():
                raise ValueError(f"SQLite database parent directory does not exist: {database_path.parent}")
            self._database = str(database_path)
            self._uri = False
            schema_connection = self._open_connection()

        try:
            schema_connection.executescript(_SCHEMA)
            schema_connection.commit()
        finally:
            if schema_connection is not self._keeper:
                schema_connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            timeout=30.0,
            uri=self._uri,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _adapt_params(params: dict | None) -> dict[str, Any]:
        adapted: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if isinstance(value, (dict, list)):
                adapted[key] = json.dumps(value, ensure_ascii=False, default=str)
            elif isinstance(value, bool):
                adapted[key] = int(value)
            else:
                adapted[key] = value
        return adapted

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for column in _JSON_COLUMNS.intersection(result):
            value = result[column]
            if isinstance(value, str):
                result[column] = json.loads(value)
        if result.get("assessor_edited") is not None:
            result["assessor_edited"] = bool(result["assessor_edited"])
        return result

    @staticmethod
    def _query_for_sqlite(query: str) -> str:
        # Keep the domain repository's parameterized PostgreSQL expression
        # backend-neutral at its public boundary while translating the one
        # dialect-specific lookback predicate at this adapter boundary.
        return _POSTGRES_LOOKBACK.sub(
            "julianday(created_at) >= julianday('now') - :lookback_days",
            query,
        )

    def fetch_one(self, query: str, params: dict | None = None) -> dict | None:
        connection = self._open_connection()
        try:
            row = connection.execute(self._query_for_sqlite(query), self._adapt_params(params)).fetchone()
            return self._row_to_dict(row) if row is not None else None
        finally:
            connection.close()

    def fetch_many(self, query: str, params: dict | None = None) -> list[dict]:
        connection = self._open_connection()
        try:
            rows = connection.execute(self._query_for_sqlite(query), self._adapt_params(params)).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def execute(self, query: str, params: dict | None = None) -> Any:
        connection = self._open_connection()
        try:
            cursor = connection.execute(self._query_for_sqlite(query), self._adapt_params(params))
            connection.commit()
            return cursor.rowcount
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        """Release the keeper connection used by the in-memory variant."""

        with self._close_lock:
            if self._keeper is not None:
                self._keeper.close()
                self._keeper = None
