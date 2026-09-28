"""AgentCore Platform v1.0 — INS-C2-906 inner: parse_rows (ingest)"""

# docs/02_design.md §3 / §7-3 — parse the raw ingest_rows payload (already
# deserialized from JSON row dicts by parse_payload) into a normalized
# working list, tracking any row-level parse failures without aborting the
# whole batch (row-unit commit contract, design §7-3).

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class ParseRowsNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state) -> dict:
        rows = state.get("ingest_rows", [])
        parsed = []
        parse_errors = []
        for i, row in enumerate(rows):
            if isinstance(row, dict):
                parsed.append(row)
            else:
                parse_errors.append({"row": i, "reason": "row is not a mapping"})

        emit_trace_event("ingest_rows_parsed", {"row_count": len(rows), "parse_error_count": len(parse_errors)}, state)

        result: dict = {"ingest_rows": parsed, "status": AgentStatus.SUCCESS.value}
        summary = dict(state.get("ingest_summary", {}))
        summary["parse_errors"] = parse_errors
        result["ingest_summary"] = summary
        return result
