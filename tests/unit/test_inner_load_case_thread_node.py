# INS-C2-906 — Unit tests: LoadCaseThreadNode (inner, design §2-3/§8/§13-1)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.load_case_thread_node import LoadCaseThreadNode


class TestLoadCaseThreadNode:
    def test_new_case_no_db_dependency(self, base_state, case_service):
        node = LoadCaseThreadNode(case_service=case_service)
        state = {**base_state, "case_id": "brand-new-uuid", "is_recheck": False}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["revision"] == 1
        assert result["prior_flags"] == []

    def test_new_case_works_even_without_case_service(self, base_state):
        """A brand-new case never touches the DB — must succeed with case_service=None."""
        node = LoadCaseThreadNode(case_service=None)
        state = {**base_state, "case_id": "brand-new-uuid", "is_recheck": False}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["revision"] == 1

    def test_recheck_unknown_case_id_errors(self, base_state, case_service):
        node = LoadCaseThreadNode(case_service=case_service)
        state = {**base_state, "case_id": "does-not-exist", "is_recheck": True}
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "unknown_case_id" in result["error_log"][0]

    def test_recheck_existing_case_loads_prior_flags_and_increments_revision(self, base_state, case_service):
        case_service.seed(
            {
                "case_id": "case-1",
                "revision": 1,
                "consistency_flags": [{"kind": "LV1_MISSING_FIELD", "flag_key": "K1", "verdict": None}],
                "final_verdict": "needs_review",
            }
        )
        node = LoadCaseThreadNode(case_service=case_service)
        state = {**base_state, "case_id": "case-1", "is_recheck": True}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["revision"] == 2
        assert result["prior_flags"] == [{"kind": "LV1_MISSING_FIELD", "flag_key": "K1", "verdict": None}]

    def test_recheck_without_case_service_fails_fast_not_new_case(self, base_state):
        """design §2-3/§8: never silently fall back to new-case behavior on
        a recheck request when the case service is unavailable."""
        node = LoadCaseThreadNode(case_service=None)
        state = {**base_state, "case_id": "case-1", "is_recheck": True}
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "case_thread_unavailable" in result["error_log"][0]

    def test_db_unreachable_on_recheck_fails_fast_not_new_case(self, base_state, case_service):
        """design §2-3/§8: a DB outage during a RECHECK must fail fast, never
        be mistaken for a new case."""
        case_service.fail_reads = True
        node = LoadCaseThreadNode(case_service=case_service)
        state = {**base_state, "case_id": "case-1", "is_recheck": True}
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "case_thread_unavailable" in result["error_log"][0]
