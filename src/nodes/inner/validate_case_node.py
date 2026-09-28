"""AgentCore Platform v1.0 — INS-C2-906 inner: validate_case (feedback)"""

# docs/02_design.md §3 / §7-2 — verify case_id exists (unknown_case_id if
# not); resolve target revision (default = latest; explicit `revision` in
# feedback_payload targets a specific row, unknown_revision if it does not
# exist for the case).

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.assessment_case_service import AssessmentCaseService


class ValidateCaseNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, case_service: AssessmentCaseService | None = None) -> None:
        self._case_service = case_service

    def execute(self, state) -> dict:
        payload = state.get("feedback_payload", {})
        case_id = payload.get("case_id", "")
        requested_revision = payload.get("revision")

        if self._case_service is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateCaseNode: no case service configured — cannot validate feedback target"],
            }

        if requested_revision is not None:
            target = self._case_service.get_revision(case_id, int(requested_revision))
            if target is None:
                emit_trace_event("case_validation_failed", {"case_id": case_id, "error": "unknown_revision"}, state)
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"ValidateCaseNode: unknown_revision {requested_revision} for case_id '{case_id}'"],
                }
            resolved_revision = int(requested_revision)
        else:
            target = self._case_service.get_latest_revision(case_id)
            if target is None:
                emit_trace_event("case_validation_failed", {"case_id": case_id, "error": "unknown_case_id"}, state)
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"ValidateCaseNode: unknown_case_id '{case_id}'"],
                }
            resolved_revision = int(target.get("revision", 1))

        emit_trace_event("case_validated", {"case_id": case_id, "revision": resolved_revision}, state)

        new_payload = dict(payload)
        new_payload["resolved_revision"] = resolved_revision
        return {"feedback_payload": new_payload, "status": AgentStatus.SUCCESS.value}
