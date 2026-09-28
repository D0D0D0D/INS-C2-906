"""AgentCore Platform v1.0 — INS-C2-906 inner: minimize_normalize (ingest)"""

# docs/02_design.md §3 / §6-3 / §7-3 — apply the column mapping to project
# each row onto canonical, coded fields only (PII/medical-text minimization
# — never store free text), and compute source_row_hash per row for
# duplicate-reingest detection.

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.ingest_utils import compute_source_row_hash


class MinimizeNormalizeNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state) -> dict:
        rows = state.get("ingest_rows", [])
        summary = state.get("ingest_summary", {})
        mapping = summary.get("column_mapping", {})

        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            normalized: dict[str, Any] = {}
            for source_col, canonical_field in mapping.items():
                if canonical_field and source_col in row:
                    normalized[canonical_field] = row[source_col]
            normalized["source_row_hash"] = compute_source_row_hash(normalized)
            normalized_rows.append(normalized)

        emit_trace_event("ingest_rows_normalized", {"row_count": len(normalized_rows)}, state)

        return {"ingest_rows": normalized_rows, "status": AgentStatus.SUCCESS.value}
