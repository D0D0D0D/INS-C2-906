# INS-C2-906 — PB-3: L1 -> External service (assessment_cases DB) real data retrieval
#
# CI has no live tenant DB, so this PB exercises the full contract through
# AssessmentCaseService's public interface with an in-memory implementation
# that satisfies the same Protocol as SqlAssessmentCaseService — the node
# layer is fully decoupled from the concrete DB backend (dependency
# injection, not construction inside execute()), which is what this PB
# actually verifies: real (non-mocked-inline) data flows end-to-end through
# the node -> service -> "external" retrieval boundary.

from src.nodes.inner.load_case_thread_node import LoadCaseThreadNode
from src.nodes.inner.lv3_anomaly_node import Lv3AnomalyNode
from src.services.assessment_case_service import AssessmentCaseService


class TestPB3ExternalServiceRetrieval:
    def test_case_service_satisfies_protocol(self, case_service):
        assert isinstance(case_service, AssessmentCaseService)

    def test_load_case_thread_retrieves_real_seeded_data(self, base_state, case_service):
        case_service.seed(
            {
                "case_id": "case-42",
                "revision": 3,
                "consistency_flags": [{"kind": "LV1_MISSING_FIELD", "flag_key": "K", "verdict": "valid"}],
                "final_verdict": "paid",
            }
        )
        node = LoadCaseThreadNode(case_service=case_service)
        result = node.execute({**base_state, "case_id": "case-42", "is_recheck": True})
        assert result["revision"] == 4  # max(3)+1
        assert result["prior_flags"][0]["flag_key"] == "K"

    def test_lv3_anomaly_retrieves_real_population_data(self, base_state, case_service):
        for i in range(35):
            case_service.seed(
                {
                    "case_id": f"pop-{i}",
                    "revision": 1,
                    "policy_type": "auto",
                    "coverage_item": "collision",
                    "claimed_amount": 1000.0 + i,
                    "final_verdict": "paid",
                    "claimant_key": "",
                }
            )
        node = Lv3AnomalyNode(case_service=case_service, min_population_n=30)
        state = {
            **base_state,
            "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
            "documents": [{"doc_id": "d1", "fields": {"claimed_amount": 1017.5}}],
            "case_id": "current-case",
            "claimant_key": "",
        }
        result = node.execute(state)
        assert result["lv3_result"]["population_n"] == 35
        assert result["lv3_result"]["degraded"] is None
