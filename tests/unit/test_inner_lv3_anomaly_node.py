# INS-C2-906 — Unit tests: Lv3AnomalyNode (inner, design §5-3/§13-1)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.lv3_anomaly_node import Lv3AnomalyNode


def _seed_population(case_service, n: int, policy_type="auto", coverage_item="collision", exclude_case_id=None):
    for i in range(n):
        case_service.seed(
            {
                "case_id": exclude_case_id if False else f"other-case-{i}",
                "revision": 1,
                "policy_type": policy_type,
                "coverage_item": coverage_item,
                "claimed_amount": 1000.0 + i,
                "final_verdict": "paid",
                "claimant_key": f"claimant-{i}",
            }
        )


class TestLv3AnomalyNode:
    def test_no_case_service_degrades_db_unavailable(self, base_state):
        node = Lv3AnomalyNode(case_service=None)
        state = {**base_state, "contract": {"policy_type": "auto", "coverage_items": ["collision"]}, "documents": [], "case_id": "case-1", "claimant_key": ""}
        result = node.execute(state)
        assert result["lv3_result"]["degraded"] == "db_unavailable"
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_insufficient_population_below_min_n(self, base_state, case_service):
        _seed_population(case_service, n=5)
        node = Lv3AnomalyNode(case_service=case_service, min_population_n=30)
        state = {
            **base_state,
            "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
            "documents": [{"doc_id": "d1", "fields": {"claimed_amount": 1000}}],
            "case_id": "case-1",
            "claimant_key": "",
        }
        result = node.execute(state)
        assert result["lv3_result"]["degraded"] == "insufficient_population"

    def test_sufficient_population_yields_score(self, base_state, case_service):
        _seed_population(case_service, n=35)
        node = Lv3AnomalyNode(case_service=case_service, min_population_n=30)
        state = {
            **base_state,
            "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
            "documents": [{"doc_id": "d1", "fields": {"claimed_amount": 1000}}],
            "case_id": "case-1",
            "claimant_key": "",
        }
        result = node.execute(state)
        assert result["lv3_result"]["population_n"] == 35
        assert result["lv3_result"]["degraded"] is None

    def test_excludes_current_case_id_revisions_from_population(self, base_state, case_service):
        """design §5-3: current case_id's own revisions excluded from
        population and similar cases (self-containment bias avoidance)."""
        _seed_population(case_service, n=35)
        # Seed a confirmed record for the CURRENT case_id — must be excluded.
        case_service.seed(
            {
                "case_id": "case-1",
                "revision": 1,
                "policy_type": "auto",
                "coverage_item": "collision",
                "claimed_amount": 999999.0,
                "final_verdict": "paid",
                "claimant_key": "",
            }
        )
        node = Lv3AnomalyNode(case_service=case_service, min_population_n=30)
        state = {
            **base_state,
            "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
            "documents": [{"doc_id": "d1", "fields": {"claimed_amount": 1000}}],
            "case_id": "case-1",
            "claimant_key": "",
        }
        result = node.execute(state)
        assert result["lv3_result"]["population_n"] == 35  # not 36 — self excluded
        similar_case_ids = {c["case_id"] for c in result["lv3_result"]["similar_cases"]}
        assert "case-1" not in similar_case_ids

    def test_repeat_claim_counts_distinct_case_id(self, base_state, case_service):
        """design §5-3/§13-1: repeat-claim count is distinct case_id, not
        revision-inflated."""
        case_service.seed(
            {
                "case_id": "case-A",
                "revision": 1,
                "policy_type": "auto",
                "coverage_item": "collision",
                "claimed_amount": 500,
                "final_verdict": "needs_review",
                "claimant_key": "shared-claimant",
            }
        )
        case_service.seed(
            {
                "case_id": "case-A",
                "revision": 2,
                "policy_type": "auto",
                "coverage_item": "collision",
                "claimed_amount": 500,
                "final_verdict": "needs_review",
                "claimant_key": "shared-claimant",
            }
        )
        case_service.seed(
            {
                "case_id": "case-B",
                "revision": 1,
                "policy_type": "auto",
                "coverage_item": "collision",
                "claimed_amount": 500,
                "final_verdict": "needs_review",
                "claimant_key": "shared-claimant",
            }
        )
        node = Lv3AnomalyNode(case_service=case_service, min_population_n=30)
        state = {
            **base_state,
            "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
            "documents": [{"doc_id": "d1", "fields": {"claimed_amount": 1000}}],
            "case_id": "case-C",
            "claimant_key": "shared-claimant",
        }
        result = node.execute(state)
        assert result["lv3_result"]["repeat_claim"]["distinct_case_count"] == 2
        assert result["lv3_result"]["repeat_claim"]["record_count"] == 3
