"""AgentCore Platform v1.0 — INS-C2-906 inner: lv1_deterministic"""

# docs/02_design.md §5-1 — pure computation, no LLM. Delegates to
# src/services/lv1_checks.py (independently unit-testable pure function).

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.lv1_checks import run_lv1_checks


class Lv1DeterministicNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state) -> dict:
        contract = state.get("contract", {})
        documents = state.get("documents", [])

        outcome = run_lv1_checks(contract, documents)

        emit_trace_event(
            "lv1_check_completed",
            {"flag_count": len(outcome["flags"]), "checked_item_count": len(outcome["checked_items"])},
            state,
        )

        return {
            "lv1_flags": outcome["flags"],
            "checked_items": outcome["checked_items"],
            "status": AgentStatus.SUCCESS.value,
        }
