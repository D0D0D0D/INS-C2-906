# INS-C2-906 — Unit tests: ValidateCaseNode + ApplyFeedbackNode (inner, design §7-2/§13-1)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.apply_feedback_node import ApplyFeedbackNode
from src.nodes.inner.validate_case_node import ValidateCaseNode


class TestValidateCaseNode:
    def test_unknown_case_id_errors(self, base_state, case_service):
        node = ValidateCaseNode(case_service=case_service)
        state = {**base_state, "feedback_payload": {"case_id": "nope"}}
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "unknown_case_id" in result["error_log"][0]

    def test_default_targets_latest_revision(self, base_state, case_service):
        case_service.seed({"case_id": "case-1", "revision": 1, "final_verdict": "needs_review"})
        case_service.seed({"case_id": "case-1", "revision": 2, "final_verdict": "needs_review"})
        node = ValidateCaseNode(case_service=case_service)
        state = {**base_state, "feedback_payload": {"case_id": "case-1"}}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["feedback_payload"]["resolved_revision"] == 2

    def test_explicit_revision_targeting(self, base_state, case_service):
        case_service.seed({"case_id": "case-1", "revision": 1, "final_verdict": "needs_review"})
        case_service.seed({"case_id": "case-1", "revision": 2, "final_verdict": "needs_review"})
        node = ValidateCaseNode(case_service=case_service)
        state = {**base_state, "feedback_payload": {"case_id": "case-1", "revision": 1}}
        result = node.execute(state)
        assert result["feedback_payload"]["resolved_revision"] == 1

    def test_unknown_revision_errors(self, base_state, case_service):
        case_service.seed({"case_id": "case-1", "revision": 1, "final_verdict": "needs_review"})
        node = ValidateCaseNode(case_service=case_service)
        state = {**base_state, "feedback_payload": {"case_id": "case-1", "revision": 99}}
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "unknown_revision" in result["error_log"][0]

    def test_no_case_service_errors(self, base_state):
        node = ValidateCaseNode(case_service=None)
        state = {**base_state, "feedback_payload": {"case_id": "case-1"}}
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value


class TestApplyFeedbackNode:
    def test_update_final_verdict(self, base_state, case_service):
        case_service.seed({"case_id": "case-1", "revision": 1, "final_verdict": "needs_review"})
        node = ApplyFeedbackNode(case_service=case_service)
        state = {
            **base_state,
            "feedback_payload": {
                "case_id": "case-1",
                "resolved_revision": 1,
                "final_verdict": "paid",
                "flag_verdicts": {},
                "assessor_edited": True,
                "action": "update",
            },
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert case_service.get_revision("case-1", 1)["final_verdict"] == "paid"

    def test_idempotent_same_payload_same_end_state(self, base_state, case_service):
        """design §7-2: same payload resent -> same end state (idempotent)."""
        case_service.seed({"case_id": "case-1", "revision": 1, "final_verdict": "needs_review"})
        node = ApplyFeedbackNode(case_service=case_service)
        payload = {
            "case_id": "case-1",
            "resolved_revision": 1,
            "final_verdict": "paid",
            "flag_verdicts": {"K1": "dismissed"},
            "assessor_edited": True,
            "action": "update",
        }
        state = {**base_state, "feedback_payload": payload}

        node.execute(state)
        row_after_first = dict(case_service.get_revision("case-1", 1))
        node.execute(state)
        row_after_second = dict(case_service.get_revision("case-1", 1))

        # updated_at will differ (timestamp bump) but all business fields converge
        row_after_first.pop("updated_at", None)
        row_after_second.pop("updated_at", None)
        assert row_after_first == row_after_second

    def test_different_verdict_resend_last_write_wins(self, base_state, case_service):
        case_service.seed({"case_id": "case-1", "revision": 1, "final_verdict": "needs_review"})
        node = ApplyFeedbackNode(case_service=case_service)

        state1 = {
            **base_state,
            "feedback_payload": {
                "case_id": "case-1",
                "resolved_revision": 1,
                "final_verdict": "denied",
                "flag_verdicts": {},
                "assessor_edited": True,
                "action": "update",
            },
        }
        node.execute(state1)
        assert case_service.get_revision("case-1", 1)["final_verdict"] == "denied"

        state2 = {**state1, "feedback_payload": {**state1["feedback_payload"], "final_verdict": "paid"}}
        node.execute(state2)
        assert case_service.get_revision("case-1", 1)["final_verdict"] == "paid"

    def test_delete_action_removes_row(self, base_state, case_service):
        case_service.seed({"case_id": "case-1", "revision": 1, "final_verdict": "paid"})
        node = ApplyFeedbackNode(case_service=case_service)
        state = {
            **base_state,
            "feedback_payload": {"case_id": "case-1", "resolved_revision": 1, "action": "delete"},
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert case_service.get_revision("case-1", 1) is None
