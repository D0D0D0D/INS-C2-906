"""AgentCore Platform v1.0 — INS-C2-906 inner: load_case_thread"""

# docs/02_design.md §2-3 / §3 / §7-1 (1)(4) / §8 — first assess-path node.
#
# - case_id supplied by caller (recheck, is_recheck=True — set by
#   pre_process BEFORE it assigns a fresh UUID, so "new vs. recheck" is
#   never inferred from a DB query result): verify existence
#   (unknown_case_id if not) -> load prior revision's coded flags + verdicts
#   into prior_flags -> provisionally assign revision = max(revision)+1
#   (confirmed only at persist_case INSERT success — §7-1 (4)).
# - case_id NOT supplied by caller (new case, is_recheck=False): pre_process
#   already assigned a fresh UUID. revision=1, no prior_flags, no DB
#   dependency.
# - DB unreachable while resolving a RECHECK: fail-fast per design §2-3/§8 —
#   never silently treat "unreachable" as "new case". This node signals
#   case_thread_unavailable and does not proceed to the rest of the assess
#   pipeline.

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.assessment_case_service import AssessmentCaseService


class LoadCaseThreadNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, case_service: AssessmentCaseService | None = None) -> None:
        self._case_service = case_service

    def execute(self, state) -> dict:
        case_id = state.get("case_id", "")
        is_recheck = bool(state.get("is_recheck", False))

        if not is_recheck:
            # New case: pre_process already assigned a fresh UUID. No DB
            # dependency for a brand-new case — revision=1, no prior_flags.
            emit_trace_event("case_thread_loaded", {"case_id": case_id, "revision": 1, "new_case": True}, state)
            return {"revision": 1, "prior_flags": [], "status": AgentStatus.SUCCESS.value}

        if self._case_service is None:
            # No case service configured but a recheck was requested —
            # fail-fast per design §2-3/§8 (never silently treat as new case).
            emit_trace_event("case_thread_loaded", {"case_id": case_id, "error": "case_thread_unavailable"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "LoadCaseThreadNode: case_thread_unavailable — no case service configured for a "
                    "recheck request. Not falling back to new-case behavior (design §2-3/§8)."
                ],
            }

        try:
            latest = self._case_service.get_latest_revision(case_id)
        except Exception as e:  # noqa: BLE001 — DB unreachable on recheck: fail-fast, no fallback
            emit_trace_event("case_thread_loaded", {"case_id": case_id, "error": "case_thread_unavailable"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"LoadCaseThreadNode: case_thread_unavailable — DB unreachable while resolving "
                    f"case_id '{case_id}' for recheck ({e}). Not falling back to new-case behavior."
                ],
            }

        if latest is None:
            emit_trace_event("case_thread_loaded", {"case_id": case_id, "error": "unknown_case_id"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"LoadCaseThreadNode: unknown_case_id '{case_id}' — recheck target does not exist"],
            }

        prior_revision = int(latest.get("revision", 0))
        prior_flags = latest.get("consistency_flags", []) or []
        provisional_revision = prior_revision + 1

        emit_trace_event(
            "case_thread_loaded",
            {"case_id": case_id, "revision": provisional_revision, "new_case": False},
            state,
        )

        return {
            "revision": provisional_revision,  # provisional — confirmed only at persist_case INSERT success
            "prior_flags": prior_flags,
            "status": AgentStatus.SUCCESS.value,
        }
