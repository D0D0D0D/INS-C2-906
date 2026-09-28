"""AgentCore Platform v1.0 — INS-C2-906 inner: compose_findings"""

# docs/02_design.md §3 — integrates Lv1/Lv2/Lv3 results + checked_items +
# revision_diff (already confirmed by persist_case) into a structured
# findings dict for get_output(). Text drafting (Markdown) happens in the
# OUTER OpinionComposeNode (post_process) — this node only assembles
# structured data, it does not write prose (design: "所見の文章化は外側
# post_process。差分の計算はしない（表示整形のみ）").

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class ComposeFindingsNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state) -> dict:
        findings = {
            "case_id": state.get("case_id", ""),
            "revision": state.get("revision", 1),
            "revision_diff": state.get("revision_diff", {}),
            "lv1_flags": state.get("lv1_flags", []),
            "lv2_flags": state.get("lv2_flags", []),
            "lv3_result": state.get("lv3_result", {}),
            "checked_items": state.get("checked_items", []),
            "persistence_status": state.get("persistence_status", "skipped"),
        }

        emit_trace_event(
            "findings_composed",
            {
                "case_id": findings["case_id"],
                "revision": findings["revision"],
                "flag_count": len(findings["lv1_flags"]) + len(findings["lv2_flags"]),
            },
            state,
        )

        return {"result": findings, "status": AgentStatus.SUCCESS.value}
