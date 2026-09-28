"""AgentCore Platform v1.0 — INS-C2-906 ClaimWorkflowGraphNode (main, GraphNode)"""

# docs/02_design.md §3 — GraphNode contract for the outer main slot:
#   - get_subgraph(): cache the inner graph in __init__ (never rebuild per
#     execute() — a known reentrancy bug pattern; also: the graph must be
#     compiled once, or every invoke() would recompile the StateGraph).
#   - extract_input(): outer State's validated_input (already a JSON string
#     built by pre_process) — passed straight through to inner invoke().
#   - merge_output(): map inner get_output() fields to outer State delta
#     keys ONLY (never full state).
#   - error_strategy = "handle": inner unexpected exceptions degrade
#     gracefully to status=error via on_subgraph_error() (design §3/§8).
#   - propagate_hitl = False: no HITL in this template (design §4-3).
#
# S-2/S-3 on this node are intentional no-op passthrough — the
# outer pre_process / post_process FunctionNodes gate input/output; this
# GraphNode boundary operates on structured dicts, not raw user text.

from __future__ import annotations

from typing import ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class ClaimWorkflowGraphNode(GraphNode):
    """main — wraps the inner ClaimCheckWorkflowGraph."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, llm=None, case_service=None, lv3_config: dict | None = None) -> None:
        self._llm = llm
        self._case_service = case_service
        self._lv3_config = lv3_config or {}
        # Cached at construction time — get_subgraph() must NEVER build a
        # fresh inner graph per execute() (reentrancy / resume-checkpoint
        # bug pattern confirmed in prior Cat 2 templates).
        from src.graph.domain_workflow_graph import ClaimCheckWorkflowGraph

        self._subgraph = ClaimCheckWorkflowGraph(
            config={"llm": self._llm, "case_service": self._case_service, "lv3": self._lv3_config}
        )
        self._subgraph.compile()

    def get_subgraph(self):
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        return state.get("validated_input", "")

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        # S-4 (design §9): GraphNode does not override execute() (framework-
        # provided), so merge_output() — always invoked on both the success
        # and inner-invoke paths — is this node's one guaranteed execution
        # point for a domain-specific audit event. Payload is coded values
        # only (case_id, revision, status) — no PII, no document/opinion text.
        emit_trace_event(
            "claim_workflow_subgraph_merged",
            {"case_id": sub_result.get("case_id") or state.get("case_id"), "status": sub_result.get("status")},
            state,
        )
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "lv1_flags": sub_result.get("lv1_flags", []),
            "lv2_flags": sub_result.get("lv2_flags", []),
            "lv3_result": sub_result.get("lv3_result", {}),
            "checked_items": sub_result.get("checked_items", []),
            "revision": sub_result.get("revision"),
            "revision_diff": sub_result.get("revision_diff", {}),
            "persistence_status": sub_result.get("persistence_status"),
            "case_id": sub_result.get("case_id") or state.get("case_id"),
            "ingest_summary": sub_result.get("ingest_summary"),
            "feedback_result": sub_result.get("feedback_result"),
        }

    def on_subgraph_error(self, state: AgentState, error: Exception) -> dict:
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"ClaimWorkflowGraphNode: inner workflow failed — {error}"],
        }
