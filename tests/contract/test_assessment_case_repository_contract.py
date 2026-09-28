"""Backend-independent acceptance contract for assessment-case persistence.

Backend registration is confined to the fixture. Assertions use only the
public ``AssessmentCaseService`` port and its domain values.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.services.assessment_case_repository_factory import create_assessment_case_repository


@pytest.fixture()
def repository(tmp_path):
    repository = create_assessment_case_repository(str(tmp_path / "assessment-cases.sqlite3"))
    yield repository
    close = getattr(repository, "close", None)
    if close is not None:
        close()


def _record(case_id: str, revision: int, **overrides):
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "case_id": case_id,
        "revision": revision,
        "policy_type": "auto",
        "coverage_item": "collision",
        "incident_type": "rear_end",
        "injury_category": "sprain",
        "treatment_or_repair_period": 14,
        "claimed_amount": 125000.0,
        "consistency_flags": [{"kind": "date", "flag_key": "date:incident", "verdict": None}],
        "anomaly_score": 0.25,
        "final_verdict": "needs_review",
        "assessor_edited": False,
        "claimant_key": "claimant-a",
        "source_row_hash": None,
        "created_at": now,
        "updated_at": now,
    }
    record.update(overrides)
    return record


def test_revision_round_trip_latest_update_and_delete(repository):
    repository.insert_case_revision(_record("case-1", 1))
    repository.insert_case_revision(_record("case-1", 2, consistency_flags=[]))

    first = repository.get_revision("case-1", 1)
    latest = repository.get_latest_revision("case-1")
    assert first["consistency_flags"][0]["flag_key"] == "date:incident"
    assert first["assessor_edited"] is False
    assert latest["revision"] == 2

    applied = repository.update_case_verdict(
        "case-1",
        2,
        {"final_verdict": "paid", "consistency_flags": {}, "assessor_edited": True},
    )
    updated = repository.get_revision("case-1", 2)
    assert applied["final_verdict"] == "paid"
    assert updated["final_verdict"] == "paid"
    assert updated["consistency_flags"] == {}
    assert updated["assessor_edited"] is True

    repository.delete_case_revision("case-1", 1)
    assert repository.get_revision("case-1", 1) is None


def test_population_contract_filters_confirmation_and_current_case(repository):
    repository.insert_case_revision(_record("paid", 1, final_verdict="paid"))
    repository.insert_case_revision(_record("partial", 1, final_verdict="partial"))
    repository.insert_case_revision(_record("denied", 1, final_verdict="denied"))
    repository.insert_case_revision(_record("pending", 1, final_verdict="needs_review"))
    repository.insert_case_revision(_record("other-product", 1, coverage_item="theft", final_verdict="paid"))

    population = repository.query_population("auto", "collision", exclude_case_id="paid")
    assert {row["case_id"] for row in population} == {"partial", "denied"}


def test_claimant_lookback_contract(repository):
    now = datetime.now(timezone.utc)
    repository.insert_case_revision(_record("recent", 1, created_at=(now - timedelta(days=2)).isoformat()))
    repository.insert_case_revision(_record("expired", 1, created_at=(now - timedelta(days=40)).isoformat()))
    repository.insert_case_revision(_record("different", 1, claimant_key="claimant-b"))

    records = repository.query_claimant_records("claimant-a", lookback_days=30)
    assert {row["case_id"] for row in records} == {"recent"}


def test_sparse_ingest_is_deduplicated_and_visible_to_reads(repository):
    row = {
        "case_id": "historical-1",
        "policy_type": "auto",
        "coverage_item": "collision",
        "final_verdict": "paid",
        "source_row_hash": "source-hash-1",
    }

    assert repository.insert_ingest_row(row) is True
    assert repository.insert_ingest_row(row) is False
    assert repository.row_hash_exists("source-hash-1") is True
    assert repository.get_revision("historical-1", 1)["policy_type"] == "auto"
