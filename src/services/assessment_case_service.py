"""AgentCore Platform v1.0 — INS-C2-906 assessment_cases DB service"""

# docs/02_design.md §6-3 / §7-1 — the `assessment_cases` table (tenant-scoped
# DB, see ASSESSMENT_DB_PATH). Stores ONLY coded values and numbers — never
# document text, free-text opinion, or raw claimant identifiers (§6-3).
#
# unique key: (case_id, revision). One revision = one INSERT (append-only —
# past revision rows are never UPDATEd by assess mode; only /feedback UPDATEs
# a specific revision row's final_verdict / flag verdicts / assessor_edited).
#
# This service wraps shared.services.database.base_db.BaseDatabase (an ABC
# with fetch_one/fetch_many/execute) behind a narrow, domain-shaped
# interface so nodes never construct raw SQL inline and tests can inject an
# in-memory fake without touching a real DB (design intent: "wire_services()
# at server startup" — dependency injection, not construction inside
# execute()).
#
# Schema (conceptual — the concrete BaseDatabase implementation owns DDL):
#   case_id, revision (PK: case_id+revision), policy_type, coverage_item,
#   incident_type, injury_category, treatment_or_repair_period,
#   claimed_amount, consistency_flags (json: [{kind, flag_key, verdict}]),
#   anomaly_score, final_verdict, assessor_edited, claimant_key,
#   source_row_hash (ingest only), created_at, updated_at.
#
# NOTE: no document-body / free-text-opinion column exists anywhere in this
# schema by design (§6-3) — tests/proof_of_boundary assert this structurally.

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from shared.services.database.base_db import BaseDatabase

CONFIRMED_VERDICTS: frozenset[str] = frozenset({"paid", "partial", "denied"})


class DuplicateRevisionError(Exception):
    """Raised when an INSERT violates the (case_id, revision) unique constraint."""


class UnknownCaseError(Exception):
    """Raised when a case_id does not exist in assessment_cases."""


class UnknownRevisionError(Exception):
    """Raised when a specific (case_id, revision) does not exist."""


class CaseThreadUnavailableError(Exception):
    """Raised when the DB is unreachable during a recheck (case_id given) —
    fail-fast per design §2-3/§8; callers must NOT fall back to new-case
    behavior on this error."""


class ReadOnlyStoreError(Exception):
    """Raised when a deployment policy refuses a persistence operation."""


@runtime_checkable
class AssessmentCaseService(Protocol):
    """Narrow domain interface the nodes depend on (for test fakes)."""

    def get_latest_revision(self, case_id: str) -> dict[str, Any] | None: ...

    def get_revision(self, case_id: str, revision: int) -> dict[str, Any] | None: ...

    def insert_case_revision(self, record: dict[str, Any]) -> None: ...

    def update_case_verdict(self, case_id: str, revision: int, updates: dict[str, Any]) -> dict[str, Any]: ...

    def delete_case_revision(self, case_id: str, revision: int) -> None: ...

    def query_population(
        self, policy_type: str, coverage_item: str, exclude_case_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def query_claimant_records(self, claimant_key: str, lookback_days: int) -> list[dict[str, Any]]: ...

    def insert_ingest_row(self, record: dict[str, Any]) -> bool: ...

    def row_hash_exists(self, source_row_hash: str) -> bool: ...


class SqlAssessmentCaseService:
    """Concrete AssessmentCaseService backed by a shared.services.database.BaseDatabase.

    Wraps generic fetch_one/fetch_many/execute with domain-shaped queries.
    Concrete DB connectivity is provided by the injected BaseDatabase
    implementation — this class owns no connection details.
    """

    def __init__(self, db: BaseDatabase):
        self._db = db

    def get_latest_revision(self, case_id: str) -> dict[str, Any] | None:
        return self._db.fetch_one(
            "SELECT * FROM assessment_cases WHERE case_id = :case_id "
            "ORDER BY revision DESC LIMIT 1",
            {"case_id": case_id},
        )

    def get_revision(self, case_id: str, revision: int) -> dict[str, Any] | None:
        return self._db.fetch_one(
            "SELECT * FROM assessment_cases WHERE case_id = :case_id AND revision = :revision",
            {"case_id": case_id, "revision": revision},
        )

    def insert_case_revision(self, record: dict[str, Any]) -> None:
        # Concrete BaseDatabase implementations are expected to raise a
        # unique-constraint violation (mapped to DuplicateRevisionError by
        # the caller's retry logic in persist_case) on (case_id, revision)
        # collision. This service does not swallow that exception.
        now = datetime.now(timezone.utc).isoformat()
        normalized = {
            "case_id": record.get("case_id") or record.get("source_row_hash", ""),
            "revision": record.get("revision", 1),
            "policy_type": record.get("policy_type", ""),
            "coverage_item": record.get("coverage_item", ""),
            "incident_type": record.get("incident_type", ""),
            "injury_category": record.get("injury_category", ""),
            "treatment_or_repair_period": record.get("treatment_or_repair_period"),
            "claimed_amount": record.get("claimed_amount"),
            "consistency_flags": record.get("consistency_flags", []),
            "anomaly_score": record.get("anomaly_score"),
            "final_verdict": record.get("final_verdict", "needs_review"),
            "assessor_edited": record.get("assessor_edited", False),
            "claimant_key": record.get("claimant_key", ""),
            "source_row_hash": record.get("source_row_hash"),
            "created_at": record.get("created_at", now),
            "updated_at": record.get("updated_at", now),
        }
        self._db.execute(
            "INSERT INTO assessment_cases "
            "(case_id, revision, policy_type, coverage_item, incident_type, "
            " injury_category, treatment_or_repair_period, claimed_amount, "
            " consistency_flags, anomaly_score, final_verdict, assessor_edited, "
            " claimant_key, source_row_hash, created_at, updated_at) "
            "VALUES (:case_id, :revision, :policy_type, :coverage_item, :incident_type, "
            " :injury_category, :treatment_or_repair_period, :claimed_amount, "
            " :consistency_flags, :anomaly_score, :final_verdict, :assessor_edited, "
            " :claimant_key, :source_row_hash, :created_at, :updated_at)",
            normalized,
        )

    def update_case_verdict(self, case_id: str, revision: int, updates: dict[str, Any]) -> dict[str, Any]:
        updates = dict(updates)
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        params = {**updates, "case_id": case_id, "revision": revision}
        self._db.execute(
            f"UPDATE assessment_cases SET {set_clause} WHERE case_id = :case_id AND revision = :revision",
            params,
        )
        return updates

    def delete_case_revision(self, case_id: str, revision: int) -> None:
        self._db.execute(
            "DELETE FROM assessment_cases WHERE case_id = :case_id AND revision = :revision",
            {"case_id": case_id, "revision": revision},
        )

    def query_population(
        self, policy_type: str, coverage_item: str, exclude_case_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM assessment_cases WHERE policy_type = :policy_type "
            "AND coverage_item = :coverage_item AND final_verdict IN ('paid', 'partial', 'denied')"
        )
        params: dict[str, Any] = {"policy_type": policy_type, "coverage_item": coverage_item}
        if exclude_case_id:
            query += " AND case_id != :exclude_case_id"
            params["exclude_case_id"] = exclude_case_id
        return self._db.fetch_many(query, params)

    def query_claimant_records(self, claimant_key: str, lookback_days: int) -> list[dict[str, Any]]:
        # `(:param || ' days')::interval` has no
        # integer||text overload in PostgreSQL and raises
        # "operator does not exist: integer || unknown" at execution time —
        # make_interval(days => :lookback_days) is the correct parameterized
        # form (no string concatenation of the bound parameter).
        return self._db.fetch_many(
            "SELECT * FROM assessment_cases WHERE claimant_key = :claimant_key "
            "AND created_at >= NOW() - make_interval(days => :lookback_days)",
            {"claimant_key": claimant_key, "lookback_days": lookback_days},
        )

    def insert_ingest_row(self, record: dict[str, Any]) -> bool:
        if self.row_hash_exists(record.get("source_row_hash", "")):
            return False
        self.insert_case_revision(record)
        return True

    def row_hash_exists(self, source_row_hash: str) -> bool:
        if not source_row_hash:
            return False
        row = self._db.fetch_one(
            "SELECT 1 AS found FROM assessment_cases WHERE source_row_hash = :source_row_hash",
            {"source_row_hash": source_row_hash},
        )
        return row is not None
