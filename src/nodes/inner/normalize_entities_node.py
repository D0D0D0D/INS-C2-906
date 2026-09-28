"""AgentCore Platform v1.0 — INS-C2-906 inner: normalize_entities"""

# docs/02_design.md §3 — normalize dates/amounts/categories extracted by
# structure_docs (Japanese-era dates -> ISO, amount units, injury category
# tagging) and retain a source pointer (doc_id + excerpt) per entity for
# Lv2's evidence requirement. This implementation performs a conservative,
# rule-based normalization (ISO date passthrough/validation, numeric
# coercion) — deeper era-date conversion is out of scope for this pass and
# is safely a no-op when values are already ISO-formatted (as structure_docs'
# prompt requests).

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_DATE_FIELDS = ("incident_date", "first_visit_date")


def _normalize_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _normalize_amount(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("¥", "").strip())
    except ValueError:
        return None


class NormalizeEntitiesNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state) -> dict:
        documents = state.get("documents", [])
        entities: list[dict[str, Any]] = []

        normalized_documents = []
        for doc in documents:
            fields = dict(doc.get("fields", {}))
            doc_id = doc.get("doc_id", "")
            doc_type = doc.get("doc_type", "")

            for date_field in _DATE_FIELDS:
                if date_field in fields:
                    normalized = _normalize_date(fields[date_field])
                    if normalized:
                        fields[date_field] = normalized
                        entities.append(
                            {"doc_id": doc_id, "doc_type": doc_type, "field": date_field, "value": normalized}
                        )

            if "claimed_amount" in fields:
                normalized_amount = _normalize_amount(fields["claimed_amount"])
                if normalized_amount is not None:
                    fields["claimed_amount"] = normalized_amount
                    entities.append(
                        {"doc_id": doc_id, "doc_type": doc_type, "field": "claimed_amount", "value": normalized_amount}
                    )

            if isinstance(fields.get("itemized_amounts"), list):
                fields["itemized_amounts"] = [
                    a for a in (_normalize_amount(v) for v in fields["itemized_amounts"]) if a is not None
                ]

            normalized_documents.append({**doc, "fields": fields})

        emit_trace_event("entities_normalized", {"entity_count": len(entities)}, state)

        return {
            "documents": normalized_documents,
            "entities": entities,
            "status": AgentStatus.SUCCESS.value,
        }
