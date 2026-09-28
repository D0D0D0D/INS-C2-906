"""Construction seam for assessment-case persistence."""

from __future__ import annotations

from shared.services.database.base_db import BaseDatabase

from src.services.assessment_case_service import AssessmentCaseService, SqlAssessmentCaseService
from src.services.sqlite_database import SQLiteDatabase


class SQLiteAssessmentCaseRepository(SqlAssessmentCaseService):
    """Domain repository backed by one process-owned SQLite store."""

    def __init__(self, database: SQLiteDatabase):
        super().__init__(database)
        self._sqlite_database = database

    def close(self) -> None:
        self._sqlite_database.close()


def create_assessment_case_repository(
    sqlite_path: str,
    *,
    database: BaseDatabase | None = None,
) -> AssessmentCaseService:
    """Build the domain repository through an explicit backend seam.

    Supplying ``database`` keeps the repository contract reusable for future
    ``BaseDatabase`` implementations. Without an override, both reads and
    writes share the same SQLite adapter instance.
    """

    if database is not None:
        if isinstance(database, SQLiteDatabase):
            return SQLiteAssessmentCaseRepository(database)
        return SqlAssessmentCaseService(database)
    return SQLiteAssessmentCaseRepository(SQLiteDatabase(sqlite_path))
