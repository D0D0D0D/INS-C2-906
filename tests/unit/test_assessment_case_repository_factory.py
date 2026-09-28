"""Tests for the assessment-case repository construction seam."""

from __future__ import annotations

from typing import Any

from shared.services.database.base_db import BaseDatabase

from src.services.assessment_case_repository_factory import (
    SQLiteAssessmentCaseRepository,
    create_assessment_case_repository,
)
from src.services.assessment_case_service import SqlAssessmentCaseService
from src.services.sqlite_database import SQLiteDatabase


class RecordingDatabase(BaseDatabase):
    """Backend fake that records port calls and parameters, not query syntax."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def fetch_one(self, query: str, params: dict | None = None) -> dict | None:
        self.calls.append(("fetch_one", dict(params or {})))
        return {"case_id": "from-injected-backend", "revision": 7}

    def fetch_many(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append(("fetch_many", dict(params or {})))
        return []

    def execute(self, query: str, params: dict | None = None) -> Any:
        self.calls.append(("execute", dict(params or {})))
        return 1


def test_non_sqlite_backend_injection_has_priority_and_receives_reads_and_writes(tmp_path):
    fallback_path = tmp_path / "must-not-be-created.sqlite3"
    database = RecordingDatabase()

    repository = create_assessment_case_repository(str(fallback_path), database=database)

    assert type(repository) is SqlAssessmentCaseService
    repository.insert_case_revision({"case_id": "written-case", "revision": 3})
    loaded = repository.get_revision("requested-case", 7)

    assert loaded == {"case_id": "from-injected-backend", "revision": 7}
    assert database.calls[0][0] == "execute"
    assert database.calls[0][1]["case_id"] == "written-case"
    assert database.calls[0][1]["revision"] == 3
    assert database.calls[1] == (
        "fetch_one",
        {"case_id": "requested-case", "revision": 7},
    )
    assert not fallback_path.exists()


def test_sqlite_backend_injection_selects_sqlite_repository_branch(tmp_path):
    fallback_path = tmp_path / "must-not-be-created.sqlite3"
    database = SQLiteDatabase(":memory:")

    repository = create_assessment_case_repository(str(fallback_path), database=database)

    assert type(repository) is SQLiteAssessmentCaseRepository
    repository.insert_case_revision({"case_id": "sqlite-case", "revision": 1})
    assert repository.get_revision("sqlite-case", 1)["case_id"] == "sqlite-case"
    assert not fallback_path.exists()
    repository.close()
