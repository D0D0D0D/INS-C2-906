"""AgentCore Platform v1.0 — INS-C2-906 ClaimConsistencyCheckAgent outer State"""

# State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption. Extend AgentState with agent-specific
# fields only. Do NOT add credentials, secrets, or Pydantic models.
#
# docs/02_design.md §4-1 — Outer State field reference.
# Constraints (mandatory, design §4-1):
#   - Flat TypedDict only (primitives + JSON-serializable types)
#   - No JWT / API keys / credential handles
#   - InvocationContext via config["configurable"] only (never in State)
#   - No Pydantic models / dataclasses / arbitrary Python objects
#   - No raw PII (names, contact info, policy numbers) — pseudonymized
#     tokens only (server.py boundary — design §6-1)

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Outer State for ClaimConsistencyCheckAgent.

    All fields below are optional at the TypedDict level (NotRequired) because
    they are populated progressively by pre_process / main / post_process —
    not all fields are present at every point in the pipeline.
    """

    # ── mode / case identity (design §4-1) ──────────────────────────────────
    mode: NotRequired[str]  # "assess" / "ingest" / "feedback"
    case_id: NotRequired[str]  # UUID v4 (new) or client-supplied (recheck/feedback)
    revision: NotRequired[int]  # case-internal revision number (assess)
    revision_diff: NotRequired[dict]  # resolved/new/continuing vs. prior revision

    # ── assess-mode payload (pseudonymized before entering the graph) ───────
    documents: NotRequired[list[dict]]  # [{doc_id, doc_type, text}, ...] — tokenized
    contract: NotRequired[dict]  # coverage_limit / deductible / policy_start / policy_end / policy_type / coverage_items
    claimant_key: NotRequired[str]  # HMAC-SHA256 one-way pseudonymized key (server.py-derived)
    validated_input: NotRequired[str]  # JSON payload handed to the inner graph (GraphNode.extract_input)

    # ── validation results ───────────────────────────────────────────────────
    lv1_flags: NotRequired[list[dict]]
    lv2_flags: NotRequired[list[dict]]
    lv3_result: NotRequired[dict]
    checked_items: NotRequired[list[dict]]
    persistence_status: NotRequired[str]  # "ok" / "failed" / "skipped" / "read_only"

    # ── ingest / feedback mode summaries ─────────────────────────────────────
    ingest_summary: NotRequired[dict]
    feedback_result: NotRequired[dict]

    # ── final composed output ────────────────────────────────────────────────
    result: NotRequired[dict]  # findings assembled by main (GraphNode.merge_output)
    output: NotRequired[dict]  # final response assembled by post_process
