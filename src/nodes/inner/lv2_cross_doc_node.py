"""AgentCore Platform v1.0 — INS-C2-906 inner: lv2_cross_doc"""

# docs/02_design.md §5-2 — LLM cross-document contradiction detection.
# temperature 0.0, structured output constrained to the fixed kind taxonomy
# (src/services/taxonomy.py). Any LLM-proposed kind outside the taxonomy is
# discarded (design: "LLM にはこの enum への構造化出力を強制し、enum 外の
# kind は破棄"). Flags without evidence are discarded here (compose-time
# defense in depth is handled again at OpinionComposeNode's S-3 hook).
#
# Scope: text-consistency only — medical plausibility / physical causation
# are explicitly out of scope (prompted against) per design §5-2.

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_client import LLMInvocationError, call_structured
from src.services.taxonomy import LV2_KIND_VALUES, compute_flag_key

_LV2_SYSTEM_PROMPT = (
    "You are checking a set of insurance claim documents for TEXTUAL inconsistencies only "
    "(dates, injury/damage descriptions, claim targets, party roles). Do NOT assess medical "
    "plausibility or physical causation. "
    "Return a JSON object: {\"flags\": [{\"kind\": <one of "
    + ", ".join(sorted(LV2_KIND_VALUES))
    + ">, \"anchors\": [\"doc_type.field\", ...], \"evidence\": "
    "[{\"doc_id\": str, \"quote\": str, \"field\": str}, ...], \"reason\": str, \"severity\": "
    "\"low\"|\"medium\"|\"high\"}]}. Every flag MUST include at least one evidence item from "
    "each of the two documents it compares. If no inconsistency is found, return {\"flags\": []}."
)


def _build_user_prompt(entities: list[dict[str, Any]], documents: list[dict[str, Any]]) -> str:
    doc_summaries = [
        {"doc_id": d.get("doc_id"), "doc_type": d.get("doc_type"), "fields": d.get("fields", {})} for d in documents
    ]
    return (
        f"Documents: {doc_summaries}\n"
        f"Normalized entities: {entities}\n"
        "Cross-reference the documents and return the flags JSON object described above."
    )


class Lv2CrossDocNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm=None) -> None:
        self._llm = llm

    def execute(self, state) -> dict:
        documents = state.get("documents", [])
        entities = state.get("entities", [])

        if self._llm is None:
            emit_trace_event("lv2_contradiction_scan_completed", {"flag_count": 0, "skipped": "no_llm"}, state)
            return {
                "lv2_flags": [],
                "status": AgentStatus.SUCCESS.value,
                "error_log": ["Lv2CrossDocNode: no LLM configured — Lv2 check skipped, Lv1 results only"],
            }

        try:
            raw = call_structured(self._llm, _LV2_SYSTEM_PROMPT, _build_user_prompt(entities, documents))
        except LLMInvocationError as e:
            emit_trace_event("lv2_contradiction_scan_completed", {"flag_count": 0, "skipped": "llm_failure"}, state)
            return {
                "lv2_flags": [],
                "status": AgentStatus.SUCCESS.value,
                "error_log": [f"Lv2CrossDocNode: LLM call failed ({e}) — Lv2 check skipped, Lv1 results only"],
            }

        accepted_flags = []
        for raw_flag in raw.get("flags", []):
            kind = raw_flag.get("kind", "")
            if kind not in LV2_KIND_VALUES:
                continue  # discard enum-external kind (design §5-2)

            evidence = raw_flag.get("evidence", [])
            if not evidence:
                continue  # discard evidence-less flags before compose (design §5-2)

            anchors = tuple(raw_flag.get("anchors", []))
            if not anchors:
                continue

            flag_key = compute_flag_key(kind, anchors)
            accepted_flags.append(
                {
                    "kind": kind,
                    "flag_key": flag_key,
                    "evidence": evidence,
                    "reason": raw_flag.get("reason", ""),
                    "severity": raw_flag.get("severity", "medium"),
                }
            )

        emit_trace_event("lv2_contradiction_scan_completed", {"flag_count": len(accepted_flags)}, state)

        return {"lv2_flags": accepted_flags, "status": AgentStatus.SUCCESS.value}
