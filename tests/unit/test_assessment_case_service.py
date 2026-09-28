# INS-C2-906 — Unit tests: SqlAssessmentCaseService query shaping (design §6-3/§7)

from unittest.mock import MagicMock

from src.services.assessment_case_service import CONFIRMED_VERDICTS, SqlAssessmentCaseService


class TestSqlAssessmentCaseService:
    def test_query_population_filters_to_confirmed_verdicts_only(self):
        """design §5-3: population must be confirmed-only (paid/partial/denied)."""
        db = MagicMock()
        db.fetch_many.return_value = []
        service = SqlAssessmentCaseService(db=db)
        service.query_population("auto", "collision")
        query = db.fetch_many.call_args[0][0]
        for verdict in CONFIRMED_VERDICTS:
            assert verdict in query
        assert "needs_review" not in query

    def test_query_population_excludes_current_case_id_when_given(self):
        db = MagicMock()
        db.fetch_many.return_value = []
        service = SqlAssessmentCaseService(db=db)
        service.query_population("auto", "collision", exclude_case_id="case-1")
        query, params = db.fetch_many.call_args[0]
        assert "exclude_case_id" in query
        assert params["exclude_case_id"] == "case-1"

    def test_insert_case_revision_uses_case_id_revision_composite(self):
        db = MagicMock()
        service = SqlAssessmentCaseService(db=db)
        service.insert_case_revision({"case_id": "c1", "revision": 1})
        query = db.execute.call_args[0][0]
        assert "case_id" in query and "revision" in query

    def test_no_document_body_or_opinion_column_in_schema(self):
        """design §6-3 structural check: no document-text / free-text-opinion
        column anywhere in the INSERT/UPDATE statements this service issues."""
        db = MagicMock()
        service = SqlAssessmentCaseService(db=db)
        service.insert_case_revision({"case_id": "c1", "revision": 1})
        insert_query = db.execute.call_args[0][0].lower()
        for forbidden in ("document_text", "doc_text", "ag_opinion", "opinion_text", "free_text"):
            assert forbidden not in insert_query

    def test_query_claimant_records_uses_make_interval_not_string_concat(self):
        """Regression guard: `(:param || ' days')::interval`
        has no integer||text overload in PostgreSQL and raises at execution
        time — make_interval(days => :param) is the correct parameterized
        form. Assert no `||` string-concatenation of the bound parameter
        remains in the query."""
        db = MagicMock()
        db.fetch_many.return_value = []
        service = SqlAssessmentCaseService(db=db)
        service.query_claimant_records("claimant-key-1", lookback_days=365)
        query, params = db.fetch_many.call_args[0]
        assert "||" not in query
        assert "make_interval" in query
        assert params["lookback_days"] == 365
