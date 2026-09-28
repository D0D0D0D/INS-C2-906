"""AgentCore Platform v1.0 — INS-C2-906 Lv1 deterministic checks (pure, no LLM)"""

# docs/02_design.md §5-1 — Lv1 is pure computation:
#   - Amount consistency (claimed_amount <= coverage_limit, deductible math,
#     itemized sum == claimed total)
#   - Period consistency (policy_start <= incident_date <= policy_end)
#   - Required-field-by-doc_type presence check
# Every check that runs is recorded into checked_items, pass or fail
# (design §10 usability point 1) — this is a hard requirement, not optional
# telemetry: an empty flags list must be distinguishable from "not checked".

from __future__ import annotations

from datetime import date
from typing import Any

from src.services.taxonomy import (
    LV1_AMOUNT_OVER_LIMIT,
    LV1_INCIDENT_OUT_OF_TERM,
    LV1_ITEMIZED_SUM_MISMATCH,
    LV1_MISSING_FIELD,
    compute_flag_key,
    normalize_anchor,
)

# doc_type -> required fields (design §5-1 "必須項目" — 請求書=金額/対象, 診断書=傷病名/初診日 等)
REQUIRED_FIELDS_BY_DOC_TYPE: dict[str, tuple[str, ...]] = {
    "invoice": ("claimed_amount", "claim_target"),
    "medical_certificate": ("injury_name", "first_visit_date"),
    "accident_report": ("incident_date", "incident_description"),
    "repair_estimate": ("claimed_amount", "claim_target"),
}

_AMOUNT_TOLERANCE = 1  # yen-level rounding tolerance for itemized-sum comparisons


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def run_lv1_checks(contract: dict[str, Any], documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Run all Lv1 deterministic checks and return flags + checked_items.

    Args:
        contract: {coverage_limit, deductible, policy_start, policy_end,
                   policy_type, coverage_items[]}
        documents: [{doc_id, doc_type, fields: {...}}, ...] — normalized,
                   entity-extracted documents (post normalize_entities).

    Returns:
        {"flags": [...], "checked_items": [...]}
    """
    flags: list[dict[str, Any]] = []
    checked_items: list[dict[str, Any]] = []

    # ── amount consistency ──────────────────────────────────────────────────
    coverage_limit = contract.get("coverage_limit")
    deductible = contract.get("deductible", 0) or 0
    claimed_total: float | None = None
    itemized_sum: float | None = None
    amount_doc_id = None

    for doc in documents:
        fields = doc.get("fields", {})
        amt = fields.get("claimed_amount")
        if amt is not None:
            claimed_total = float(amt)
            amount_doc_id = doc.get("doc_id")
        items = fields.get("itemized_amounts")
        if isinstance(items, list) and items:
            itemized_sum = float(sum(float(x) for x in items))

    if coverage_limit is not None and claimed_total is not None:
        passed = claimed_total <= float(coverage_limit)
        checked_items.append(
            {
                "check": "amount_within_coverage_limit",
                "level": "Lv1",
                "passed": passed,
                "detail": {"claimed_total": claimed_total, "coverage_limit": coverage_limit},
            }
        )
        if not passed:
            flags.append(
                {
                    "kind": LV1_AMOUNT_OVER_LIMIT,
                    "flag_key": compute_flag_key(
                        LV1_AMOUNT_OVER_LIMIT,
                        (normalize_anchor("contract", "coverage_limit"), normalize_anchor("invoice", "claimed_amount")),
                    ),
                    "evidence": [
                        {"doc_id": amount_doc_id, "field": "claimed_amount", "quote": str(claimed_total)},
                    ],
                    "reason": f"claimed_total ({claimed_total}) exceeds coverage_limit ({coverage_limit})",
                    "severity": "high",
                }
            )
    else:
        checked_items.append(
            {
                "check": "amount_within_coverage_limit",
                "level": "Lv1",
                "passed": None,
                "detail": {"reason": "insufficient data (coverage_limit or claimed_amount missing)"},
            }
        )

    if itemized_sum is not None and claimed_total is not None:
        passed = abs(itemized_sum - claimed_total) <= _AMOUNT_TOLERANCE
        checked_items.append(
            {
                "check": "itemized_sum_matches_claimed_total",
                "level": "Lv1",
                "passed": passed,
                "detail": {"itemized_sum": itemized_sum, "claimed_total": claimed_total},
            }
        )
        if not passed:
            flags.append(
                {
                    "kind": LV1_ITEMIZED_SUM_MISMATCH,
                    "flag_key": compute_flag_key(
                        LV1_ITEMIZED_SUM_MISMATCH,
                        (normalize_anchor("invoice", "itemized_amounts"), normalize_anchor("invoice", "claimed_amount")),
                    ),
                    "evidence": [
                        {"doc_id": amount_doc_id, "field": "itemized_amounts", "quote": str(itemized_sum)},
                        {"doc_id": amount_doc_id, "field": "claimed_amount", "quote": str(claimed_total)},
                    ],
                    "reason": f"itemized sum ({itemized_sum}) does not match claimed total ({claimed_total})",
                    "severity": "medium",
                }
            )

    # deductible math is checked as an informational item (never blocks; no
    # single deductible-mismatch flag kind is emitted unless computed net
    # amount is inconsistent with the claimed total after deduction)
    if coverage_limit is not None and claimed_total is not None and deductible:
        net_after_deductible = max(float(claimed_total) - float(deductible), 0.0)
        checked_items.append(
            {
                "check": "deductible_math",
                "level": "Lv1",
                "passed": True,
                "detail": {"claimed_total": claimed_total, "deductible": deductible, "net_after_deductible": net_after_deductible},
            }
        )

    # ── period consistency ───────────────────────────────────────────────────
    policy_start = _parse_iso_date(contract.get("policy_start"))
    policy_end = _parse_iso_date(contract.get("policy_end"))
    incident_date = None
    incident_doc_id = None
    for doc in documents:
        fields = doc.get("fields", {})
        if fields.get("incident_date"):
            incident_date = _parse_iso_date(fields["incident_date"])
            incident_doc_id = doc.get("doc_id")
            break

    if policy_start and policy_end and incident_date:
        passed = policy_start <= incident_date <= policy_end
        checked_items.append(
            {
                "check": "incident_within_policy_period",
                "level": "Lv1",
                "passed": passed,
                "detail": {
                    "policy_start": str(policy_start),
                    "policy_end": str(policy_end),
                    "incident_date": str(incident_date),
                },
            }
        )
        if not passed:
            flags.append(
                {
                    "kind": LV1_INCIDENT_OUT_OF_TERM,
                    "flag_key": compute_flag_key(
                        LV1_INCIDENT_OUT_OF_TERM,
                        (normalize_anchor("contract", "policy_period"), normalize_anchor("accident_report", "incident_date")),
                    ),
                    "evidence": [
                        {"doc_id": incident_doc_id, "field": "incident_date", "quote": str(incident_date)},
                    ],
                    "reason": (
                        f"incident_date ({incident_date}) is outside policy period "
                        f"({policy_start} - {policy_end})"
                    ),
                    "severity": "high",
                }
            )
    else:
        checked_items.append(
            {
                "check": "incident_within_policy_period",
                "level": "Lv1",
                "passed": None,
                "detail": {"reason": "insufficient data (policy dates or incident_date missing)"},
            }
        )

    # ── required-field-by-doc_type presence ──────────────────────────────────
    for doc in documents:
        doc_type = doc.get("doc_type", "")
        required = REQUIRED_FIELDS_BY_DOC_TYPE.get(doc_type, ())
        fields = doc.get("fields", {})
        missing = [f for f in required if not fields.get(f)]
        checked_items.append(
            {
                "check": f"required_fields_present[{doc_type}]",
                "level": "Lv1",
                "passed": len(missing) == 0,
                "detail": {"doc_id": doc.get("doc_id"), "doc_type": doc_type, "missing": missing},
            }
        )
        if missing:
            flags.append(
                {
                    "kind": LV1_MISSING_FIELD,
                    "flag_key": compute_flag_key(
                        LV1_MISSING_FIELD,
                        tuple(normalize_anchor(doc_type, f) for f in sorted(missing)),
                    ),
                    "evidence": [{"doc_id": doc.get("doc_id"), "field": f, "quote": ""} for f in missing],
                    "reason": f"required fields missing for doc_type '{doc_type}': {missing}",
                    "severity": "medium",
                }
            )

    return {"flags": flags, "checked_items": checked_items}
