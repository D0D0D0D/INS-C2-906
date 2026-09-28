"""AgentCore Platform v1.0 — INS-C2-906 inner: apply_feedback (feedback)"""

# docs/02_design.md §3 / §7-2 — idempotent UPDATE of final_verdict / flag
# verdicts / assessor_edited on the resolved (case_id, revision) row only.
# Same payload resent -> same end state (idempotent). Different verdict
# resent -> last-write-wins + updated_at bump (handled by
# AssessmentCaseService.update_case_verdict, which always sets updated_at).

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.assessment_case_service import AssessmentCaseService


class ApplyFeedbackNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, case_service: AssessmentCaseService | None = None) -> None:
        self._case_service = case_service

    def execute(self, state) -> dict:
        payload = state.get("feedback_payload", {})
        case_id = payload.get("case_id", "")
        revision = payload.get("resolved_revision")
        action = payload.get("action", "update")

        if self._case_service is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ApplyFeedbackNode: no case service configured — cannot apply feedback"],
            }

        if action == "delete":
            # Data-subject deletion route (design §6-3 "個別削除要求").
            self._case_service.delete_case_revision(case_id, int(revision))
            emit_trace_event("feedback_applied", {"case_id": case_id, "revision": revision, "action": "delete"}, state)
            return {
                "feedback_result": {"case_id": case_id, "revision": revision, "action": "delete", "status": "deleted"},
                "status": AgentStatus.SUCCESS.value,
            }

        updates = {
            "final_verdict": payload.get("final_verdict"),
            "consistency_flags": payload.get("flag_verdicts", {}),
            "assessor_edited": bool(payload.get("assessor_edited", False)),
        }
        applied = self._case_service.update_case_verdict(case_id, int(revision), updates)

        emit_trace_event("feedback_applied", {"case_id": case_id, "revision": revision, "action": "update"}, state)

        return {
            "feedback_result": {
                "case_id": case_id,
                "revision": revision,
                "action": "update",
                "applied": applied,
            },
            "status": AgentStatus.SUCCESS.value,
        }
