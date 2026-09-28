"""AgentCore Platform v1.0 — INS-C2-906 inner ClaimWorkflowState"""

# docs/02_design.md §4-2 — Inner State (ClaimCheckWorkflowGraph, src/graph/domain_workflow_graph.py).
#
# Same constraints as the outer State (design §4-1): flat TypedDict, no raw
# PII, no Pydantic/dataclass, msgpack-safe types only.
#
# IMPORTANT (design §3 / §4-2, confirmed at runtime): any conditional-branch
# function in the inner graph (mode_router, route) MUST be annotated with
# ClaimWorkflowState — NOT AgentState. Annotating with AgentState causes
# LangGraph to project the state down to AgentState's fields at the node
# boundary, silently dropping custom fields such as `mode`, and the branch
# then always falls through to the default case (mis-routing). This is
# exercised explicitly in tests/integration/test_domain_workflow_graph.py.

from typing import NotRequired, TypedDict


class ClaimWorkflowState(TypedDict):
    """Inner graph state for ClaimCheckWorkflowGraph (design §3 / §4-2).

    parse_payload is the sole node that deserializes the JSON string handed
    down by the outer GraphNode.extract_input() into these typed fields.
    """

    # ── framework-propagated context (see GraphNode.execute() / InvocationContext) ──
    correlation_id: NotRequired[str]
    session_id: NotRequired[str]
    trace_id: NotRequired[str]
    thread_id: NotRequired[str]
    caller_trust_level: NotRequired[str]
    caller_id: NotRequired[str]
    hitl_allowed: NotRequired[bool]

    # ── raw payload from the outer GraphNode (parse_payload deserializes this) ──
    user_input: NotRequired[str]  # JSON string produced by outer extract_input()

    # ── mode / case identity ─────────────────────────────────────────────────
    mode: NotRequired[str]  # "assess" / "ingest" / "feedback"
    case_id: NotRequired[str]
    is_recheck: NotRequired[bool]  # True iff the caller explicitly supplied case_id (design §2-3)
    revision: NotRequired[int]
    prior_flags: NotRequired[list[dict]]
    revision_diff: NotRequired[dict]

    # ── assess-mode working state ────────────────────────────────────────────
    documents: NotRequired[list[dict]]
    contract: NotRequired[dict]
    claimant_key: NotRequired[str]
    entities: NotRequired[list[dict]]
    lv1_flags: NotRequired[list[dict]]
    lv2_flags: NotRequired[list[dict]]
    lv3_result: NotRequired[dict]
    checked_items: NotRequired[list[dict]]
    persistence_status: NotRequired[str]

    # ── ingest-mode working state ────────────────────────────────────────────
    ingest_rows: NotRequired[list[dict]]
    ingest_summary: NotRequired[dict]

    # ── feedback-mode working state ──────────────────────────────────────────
    feedback_payload: NotRequired[dict]
    feedback_result: NotRequired[dict]

    # ── output ────────────────────────────────────────────────────────────────
    result: NotRequired[dict]
    status: NotRequired[str]
    error_log: NotRequired[list[str]]
    node_history: NotRequired[list[str]]
