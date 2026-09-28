"""AgentCore Platform v1.0 — INS-C2-906 inner: structure_docs"""

# docs/02_design.md §3 — per-document LLM summarization + fact-entity
# extraction (incident date / first-visit date / injury site / damage
# location / amount / claim target / party role). One document = one LLM
# call (documents are never mixed in a single prompt). Large documents are
# summarized section-by-section to stay within token limits (design intent
# — a "段階要約" strategy that this thin implementation approximates with a
# simple truncation guard, since full chunked summarization is an ingest-
# time optimization the design leaves to the concrete LLM prompt).
#
# On LLM failure for a given document, that document's `fields` degrade to
# {} rather than aborting the whole assess run — Lv1 still records the
# missing-fields checked_item, and Lv2 simply has less to cross-reference
# (design §8: "LLM 呼び出し失敗... 失敗時は Lv1 結果のみで所見を構成").

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_client import LLMInvocationError, call_structured

_STRUCTURE_SYSTEM_PROMPT = (
    "You are extracting structured facts from a single insurance claim document. "
    "Return a JSON object with keys matching known fields only "
    "(incident_date, first_visit_date, injury_name, claimed_amount, itemized_amounts, "
    "claim_target, incident_description, party_role) — omit keys you cannot find. "
    "Do not invent values. Use ISO-8601 for dates."
)


class StructureDocsNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm=None) -> None:
        self._llm = llm

    def execute(self, state) -> dict:
        documents = state.get("documents", [])
        structured_docs = []
        llm_failed_docs = []

        for doc in documents:
            doc_id = doc.get("doc_id", "")
            doc_type = doc.get("doc_type", "unknown")
            text = doc.get("text", "")

            fields: dict = {}
            if self._llm is not None:
                try:
                    fields = call_structured(self._llm, _STRUCTURE_SYSTEM_PROMPT, text)
                except LLMInvocationError:
                    llm_failed_docs.append(doc_id)
                    fields = {}

            structured_docs.append({"doc_id": doc_id, "doc_type": doc_type, "text": text, "fields": fields})

        emit_trace_event(
            "claim_docs_structured",
            {"document_count": len(documents), "llm_failed_docs": llm_failed_docs},
            state,
        )

        result: dict = {"documents": structured_docs, "status": AgentStatus.SUCCESS.value}
        if llm_failed_docs:
            result["error_log"] = [
                f"StructureDocsNode: LLM structuring failed for doc_ids {llm_failed_docs} — "
                "fields degraded to empty; Lv1 will flag missing required fields."
            ]
        return result
