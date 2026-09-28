"""AgentCore Platform v1.0 — INS-C2-906 inner: map_columns (ingest)"""

# docs/02_design.md §3 / §7-3 — LLM-assisted column mapping to absorb
# non-uniform CSV/Excel column layouts. The mapping result is always
# returned in the response (design §7-3 "投入前レビューのため応答に含める"),
# and `dry_run: true` performs mapping only, no downstream writes (checked
# by store_records, not here).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_client import LLMInvocationError, call_structured

_CANONICAL_COLUMNS = (
    "case_id",
    "policy_type",
    "coverage_item",
    "incident_type",
    "injury_category",
    "treatment_or_repair_period",
    "claimed_amount",
    "final_verdict",
    "claimant_identifier",
)

_MAP_COLUMNS_SYSTEM_PROMPT = (
    "Map the given source column headers to these canonical fields: "
    + ", ".join(_CANONICAL_COLUMNS)
    + ". Return a JSON object {\"mapping\": {<source_column>: <canonical_field or null>, ...}}."
)


def _heuristic_mapping(headers: list[str]) -> dict[str, str | None]:
    mapping: dict[str, str | None] = {}
    lowered = {h: h.lower().replace(" ", "_") for h in headers}
    for header, norm in lowered.items():
        match = next((c for c in _CANONICAL_COLUMNS if c in norm or norm in c), None)
        mapping[header] = match
    return mapping


class MapColumnsNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm=None) -> None:
        self._llm = llm

    def execute(self, state) -> dict:
        rows = state.get("ingest_rows", [])
        headers = list(rows[0].keys()) if rows else []

        mapping: dict[str, Any]
        if self._llm is not None:
            try:
                raw = call_structured(
                    self._llm, _MAP_COLUMNS_SYSTEM_PROMPT, f"Source columns: {headers}"
                )
                mapping = raw.get("mapping", {})
            except LLMInvocationError:
                mapping = _heuristic_mapping(headers)
        else:
            mapping = _heuristic_mapping(headers)

        emit_trace_event("ingest_columns_mapped", {"header_count": len(headers)}, state)

        summary = dict(state.get("ingest_summary", {}))
        summary["column_mapping"] = mapping
        return {"ingest_summary": summary, "status": AgentStatus.SUCCESS.value}
