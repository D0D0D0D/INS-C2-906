"""AgentCore Platform v1.0 — INS-C2-906 inner: parse_payload"""

# docs/02_design.md §3 — first inner node. Deserializes the JSON string the
# outer GraphNode.extract_input() hands down (user_input) into the typed
# ClaimWorkflowState fields (mode/case_id/documents/contract/claimant_key/
# feedback_payload/ingest rows). This is the ONLY place doing string->dict
# reconstruction — every other inner node reads already-typed state fields.

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class ParsePayloadNode(FunctionNode):
    """Deserialize outer JSON payload (user_input) into typed state fields."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict:
        raw = state.get("user_input", "")
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as e:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ParsePayloadNode: failed to parse payload — {e}"],
            }

        mode = payload.get("mode", "")
        if mode not in {"assess", "ingest", "feedback"}:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ParsePayloadNode: unknown or missing mode '{mode}'"],
            }

        result: dict = {"mode": mode, "status": AgentStatus.SUCCESS.value}

        if mode == "assess":
            case_id = payload.get("case_id", "")
            result["case_id"] = case_id
            # is_recheck distinguishes "brand-new UUID assigned by pre_process"
            # from "caller explicitly supplied an existing case_id to recheck"
            # (design §2-3). pre_process forwards this explicitly rather than
            # letting load_case_thread guess from query results, so a DB
            # outage can never be misread as "new case" (design §2-3/§8).
            result["is_recheck"] = bool(payload.get("is_recheck", False))
            result["documents"] = payload.get("documents", [])
            result["contract"] = payload.get("contract", {})
            result["claimant_key"] = payload.get("claimant_key", "")
        elif mode == "ingest":
            result["ingest_rows"] = payload.get("rows", [])
            result["ingest_summary"] = {"dry_run": bool(payload.get("dry_run", False))}
        else:  # feedback
            result["feedback_payload"] = payload.get("feedback_payload", {})
            result["case_id"] = payload.get("feedback_payload", {}).get("case_id", "")

        emit_trace_event("payload_parsed", {"mode": mode}, state)
        return result
