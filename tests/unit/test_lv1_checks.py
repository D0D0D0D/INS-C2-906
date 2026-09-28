# INS-C2-906 — Unit tests: Lv1 deterministic checks (design §5-1)

from src.services.lv1_checks import run_lv1_checks
from src.services.taxonomy import LV1_INCIDENT_OUT_OF_TERM, LV1_MISSING_FIELD


class TestLv1Checks:
    def test_all_pass_records_checked_items_with_no_flags(self):
        contract = {
            "coverage_limit": 5000,
            "deductible": 0,
            "policy_start": "2026-01-01",
            "policy_end": "2026-12-31",
        }
        documents = [
            {
                "doc_id": "d1",
                "doc_type": "invoice",
                "fields": {"claimed_amount": 1000, "claim_target": "vehicle"},
            },
            {
                "doc_id": "d2",
                "doc_type": "accident_report",
                "fields": {"incident_date": "2026-06-01", "incident_description": "collision"},
            },
        ]
        outcome = run_lv1_checks(contract, documents)
        assert outcome["flags"] == []
        # design §10 usability point 1: checked_items recorded even when all pass
        assert len(outcome["checked_items"]) > 0
        assert all(item["level"] == "Lv1" for item in outcome["checked_items"])

    def test_amount_over_limit_flags(self):
        contract = {"coverage_limit": 500}
        documents = [{"doc_id": "d1", "doc_type": "invoice", "fields": {"claimed_amount": 1000}}]
        outcome = run_lv1_checks(contract, documents)
        kinds = [f["kind"] for f in outcome["flags"]]
        assert "LV1_AMOUNT_OVER_LIMIT" in kinds

    def test_incident_out_of_policy_term_flags(self):
        contract = {"policy_start": "2026-01-01", "policy_end": "2026-03-31"}
        documents = [
            {"doc_id": "d1", "doc_type": "accident_report", "fields": {"incident_date": "2026-06-01"}}
        ]
        outcome = run_lv1_checks(contract, documents)
        kinds = [f["kind"] for f in outcome["flags"]]
        assert LV1_INCIDENT_OUT_OF_TERM in kinds

    def test_missing_required_field_flags(self):
        contract = {}
        documents = [{"doc_id": "d1", "doc_type": "medical_certificate", "fields": {}}]
        outcome = run_lv1_checks(contract, documents)
        kinds = [f["kind"] for f in outcome["flags"]]
        assert LV1_MISSING_FIELD in kinds

    def test_itemized_sum_mismatch_flags(self):
        contract = {}
        documents = [
            {
                "doc_id": "d1",
                "doc_type": "invoice",
                "fields": {"claimed_amount": 1000, "itemized_amounts": [400, 400]},
            }
        ]
        outcome = run_lv1_checks(contract, documents)
        kinds = [f["kind"] for f in outcome["flags"]]
        assert "LV1_ITEMIZED_SUM_MISMATCH" in kinds

    def test_insufficient_data_records_checked_item_with_none_passed(self):
        """Not every check can run for every input — those cases are recorded
        as passed=None (distinguishable from pass/fail), not silently skipped."""
        outcome = run_lv1_checks({}, [{"doc_id": "d1", "doc_type": "invoice", "fields": {}}])
        amount_item = next(i for i in outcome["checked_items"] if i["check"] == "amount_within_coverage_limit")
        assert amount_item["passed"] is None

    def test_flag_keys_are_deterministic_across_calls(self):
        contract = {"coverage_limit": 500}
        documents = [{"doc_id": "d1", "doc_type": "invoice", "fields": {"claimed_amount": 1000}}]
        outcome1 = run_lv1_checks(contract, documents)
        outcome2 = run_lv1_checks(contract, documents)
        assert outcome1["flags"][0]["flag_key"] == outcome2["flags"][0]["flag_key"]
