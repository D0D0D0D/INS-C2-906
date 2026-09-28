"""AgentCore Platform v1.0 — INS-C2-906 ClaimIntakeNode (pre_process)"""

# docs/02_design.md §2-1 / §6-2 — pre_process responsibilities:
#   - mode validation ("assess" / "ingest" / "feedback")
#   - required-payload-shape validation per mode
#   - input size limits (max_documents / max_total_chars)
#   - case_id assignment: UUID v4 for new assess cases (DB-independent —
#     design §7-1 (1): "新規ケースの case_id は pre_process で採番（UUID v4）
#     — DB 採番に依存しない")
#   - S-2 _extra_security_gate_input(): residual-PII rescan — server.py
#     tokenizes before the graph is invoked, but this is the last line of
#     defense inside the graph boundary; fail-closed on detection (§6-2)

from __future__ import annotations

import json
import uuid
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

VALID_MODES = frozenset({"assess", "ingest", "feedback"})

DEFAULT_MAX_DOCUMENTS = 12
DEFAULT_MAX_TOTAL_CHARS = 400_000


class ClaimIntakeNode(FunctionNode):
    """pre_process — validates mode/payload shape, enforces size limits,
    assigns case_id for new assess cases, and re-scans for residual PII.
    """

    # S-1 (design §9): standard business operation handling claim documents (PII).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
        max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    ) -> None:
        self._max_documents = max_documents
        self._max_total_chars = max_total_chars

    def execute(self, state: AgentState) -> dict:
        input_context = state.get("input_context", {})  # read-only
        mode = input_context.get("mode", "assess")

        if mode not in VALID_MODES:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ClaimIntakeNode: invalid mode '{mode}' — must be one of {sorted(VALID_MODES)}"],
            }

        if mode == "assess":
            return self._handle_assess(state, input_context)
        if mode == "ingest":
            return self._handle_ingest(state, input_context)
        return self._handle_feedback(state, input_context)

    # ------------------------------------------------------------------
    # mode handlers
    # ------------------------------------------------------------------

    def _handle_assess(self, state: AgentState, input_context: dict) -> dict:
        documents = input_context.get("documents", [])
        contract = input_context.get("contract", {})
        case_id = input_context.get("case_id") or ""
        claimant_key = input_context.get("claimant_key", "")

        if not isinstance(documents, list) or not documents:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClaimIntakeNode: assess mode requires a non-empty 'documents' list"],
            }
        if not isinstance(contract, dict) or not contract:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClaimIntakeNode: assess mode requires a 'contract' object"],
            }

        size_error = self._check_size_limits(documents)
        if size_error:
            return {"status": AgentStatus.ERROR.value, "error_log": [size_error]}

        # New case: assign case_id here (DB-independent — design §7-1 (1)).
        # Recheck: case_id is the client-supplied value, verified downstream
        # by load_case_thread (inner graph), not here. is_recheck is recorded
        # explicitly (before UUID assignment) so the inner graph never has to
        # infer "new vs. recheck" from a DB query result — a DB outage on a
        # genuine recheck therefore cannot be misread as "new case"
        # (design §2-3/§8).
        is_recheck = bool(case_id)
        assigned_case_id = case_id or str(uuid.uuid4())

        validated_input = json.dumps(
            {
                "mode": "assess",
                "case_id": assigned_case_id,
                "is_recheck": is_recheck,
                "documents": documents,
                "contract": contract,
                "claimant_key": claimant_key,
            },
            default=str,
        )

        emit_trace_event(
            "claim_intake_validated",
            {"mode": "assess", "case_id": assigned_case_id, "document_count": len(documents), "is_recheck": is_recheck},
            state,
        )

        return {
            "mode": "assess",
            "case_id": assigned_case_id,
            "documents": documents,
            "contract": contract,
            "claimant_key": claimant_key,
            "validated_input": validated_input,
            "status": AgentStatus.SUCCESS.value,
        }

    def _handle_ingest(self, state: AgentState, input_context: dict) -> dict:
        rows = input_context.get("rows", [])
        dry_run = bool(input_context.get("dry_run", False))

        if not isinstance(rows, list) or not rows:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClaimIntakeNode: ingest mode requires a non-empty 'rows' list"],
            }

        validated_input = json.dumps(
            {"mode": "ingest", "rows": rows, "dry_run": dry_run},
            default=str,
        )

        emit_trace_event("claim_intake_validated", {"mode": "ingest", "row_count": len(rows)}, state)

        return {
            "mode": "ingest",
            "validated_input": validated_input,
            "status": AgentStatus.SUCCESS.value,
        }

    def _handle_feedback(self, state: AgentState, input_context: dict) -> dict:
        case_id = input_context.get("case_id", "")
        if not case_id:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClaimIntakeNode: feedback mode requires 'case_id'"],
            }

        feedback_payload = {
            "case_id": case_id,
            "revision": input_context.get("revision"),
            "final_verdict": input_context.get("final_verdict"),
            "flag_verdicts": input_context.get("flag_verdicts", {}),
            "assessor_edited": input_context.get("assessor_edited", False),
            "action": input_context.get("action", "update"),
        }

        validated_input = json.dumps({"mode": "feedback", "feedback_payload": feedback_payload}, default=str)

        emit_trace_event("claim_intake_validated", {"mode": "feedback", "case_id": case_id}, state)

        return {
            "mode": "feedback",
            "case_id": case_id,
            "validated_input": validated_input,
            "status": AgentStatus.SUCCESS.value,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _check_size_limits(self, documents: list[dict[str, Any]]) -> str | None:
        if len(documents) > self._max_documents:
            return f"ClaimIntakeNode: document count {len(documents)} exceeds max_documents ({self._max_documents})"
        total_chars = sum(len(str(doc.get("text", ""))) for doc in documents)
        if total_chars > self._max_total_chars:
            return f"ClaimIntakeNode: total document chars {total_chars} exceeds max_total_chars ({self._max_total_chars})"
        return None

    # ------------------------------------------------------------------
    # S-2 extension hook (design §6-2)
    # ------------------------------------------------------------------

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        """Residual-PII rescan: server.py tokenizes before invoke(), but this
        is the last defense inside the graph boundary. Fail-closed on any
        detected raw PII pattern surviving tokenization (design §6-2).

        Contract note: FunctionNode._extra_security_gate_input() must not
        raise — reject via state["status"]=ERROR, per the framework hook
        contract (customize-security-gates.md). SecurityViolationError is
        reserved for S-1/S-5 framework-raised cases; this hook signals
        rejection through state, which is the sanctioned fail-closed path.
        """
        input_context = state.get("input_context", {})
        documents = input_context.get("documents", [])

        residual_findings: list[dict[str, Any]] = []
        for doc in documents:
            text = str(doc.get("text", ""))
            findings = detect_pii(text)
            # Only direct-identifier types are in scope here (medical content
            # is intentionally left untokenized upstream — design §6-1).
            residual_findings.extend(
                f for f in findings if f["type"] in {"email", "phone_jp", "phone_us", "ssn_us", "my_number_jp", "credit_card"}
            )

        if residual_findings:
            state = dict(state)
            state["status"] = AgentStatus.ERROR.value
            state["error_log"] = [
                "ClaimIntakeNode: S-2 residual PII rescan detected "
                f"{len(residual_findings)} unmasked pattern(s) in documents — "
                "tokenization boundary was bypassed. Rejecting input (fail-closed)."
            ]
            emit_trace_event(
                "s2_residual_pii_detected",
                {"finding_count": len(residual_findings), "types": sorted({f["type"] for f in residual_findings})},
                state,
            )
        return state
