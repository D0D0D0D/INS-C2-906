"""AgentCore Platform v1.0 — INS-C2-906 inner: persist_case"""

# docs/02_design.md §2-3 / §3 / §6-3 / §7-1 (3)(4)(5) — persist_case:
#
# 1. If revision >= 2: compute revision_diff (pure function, flag_key
#    matching) against the provisional prior_flags baseline from
#    load_case_thread.
# 2. Build the minimized/coded record and INSERT as a new (case_id,
#    revision) row.
# 3. On unique-constraint collision (concurrent recheck race): reload the
#    latest revision's flags -> reassign revision -> RECOMPUTE revision_diff
#    against the NEW prior baseline (no LLM re-execution needed — pure
#    function) -> retry INSERT exactly once.
# 4. If the retry also fails: persistence_status="failed", continue anyway
#    — the assessment response is never blocked by a DB write failure
#    (§7-1 (5)). Findings are still returned to the caller.
# 5. On INSERT success, emit S-4 case_revision_created with the diff summary
#    counts (the diff is confirmed at this point — design §9).
#
# This is best-effort persistence for the *write* path. Contrast with
# load_case_thread, which fails fast on a *read*-path DB outage during a
# recheck (design §2-3/§8 draws this distinction explicitly).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.assessment_case_service import AssessmentCaseService, ReadOnlyStoreError
from src.services.revision_diff import compute_revision_diff


class PersistCaseNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, case_service: AssessmentCaseService | None = None) -> None:
        self._case_service = case_service

    def execute(self, state) -> dict:
        case_id = state.get("case_id", "")
        revision = state.get("revision", 1)
        prior_flags = state.get("prior_flags", [])
        lv1_flags = state.get("lv1_flags", [])
        lv2_flags = state.get("lv2_flags", [])
        current_flags = list(lv1_flags) + list(lv2_flags)
        contract = state.get("contract", {})
        documents = state.get("documents", [])
        lv3_result = state.get("lv3_result", {})
        claimant_key = state.get("claimant_key", "")

        revision_diff: dict[str, Any] = {}
        if revision >= 2:
            revision_diff = compute_revision_diff(prior_flags, current_flags)

        if self._case_service is None:
            emit_trace_event("case_persisted", {"case_id": case_id, "revision": revision, "persistence_status": "skipped"}, state)
            return {
                "revision": revision,
                "revision_diff": revision_diff,
                "persistence_status": "skipped",
                "status": AgentStatus.SUCCESS.value,
            }

        record = self._build_record(case_id, revision, contract, documents, current_flags, lv3_result, claimant_key)

        try:
            self._case_service.insert_case_revision(record)
            persistence_status = "ok"
            final_revision = revision
            final_diff = revision_diff
        except Exception as first_error:  # noqa: BLE001 — collision or transient failure
            if isinstance(first_error, ReadOnlyStoreError):
                emit_trace_event(
                    "case_persisted",
                    {
                        "case_id": case_id,
                        "revision": revision,
                        "persistence_status": "read_only",
                    },
                    state,
                )
                return {
                    "revision": revision,
                    "revision_diff": revision_diff,
                    "persistence_status": "read_only",
                    "status": AgentStatus.SUCCESS.value,
                }
            final_revision, final_diff, persistence_status = self._retry_once(
                case_id, record, current_flags, first_error
            )

        emit_trace_event(
            "case_persisted",
            {"case_id": case_id, "revision": final_revision, "persistence_status": persistence_status},
            state,
        )
        if persistence_status == "ok":
            emit_trace_event(
                "case_revision_created",
                {"case_id": case_id, "revision": final_revision, "diff_counts": final_diff.get("counts", {})},
                state,
            )

        return {
            "revision": final_revision,
            "revision_diff": final_diff,
            "persistence_status": persistence_status,
            "status": AgentStatus.SUCCESS.value,
        }

    def _retry_once(
        self, case_id: str, record: dict[str, Any], current_flags: list[dict[str, Any]], first_error: Exception
    ) -> tuple[int, dict[str, Any], str]:
        """Unique-constraint collision retry: reload -> reassign -> recompute -> retry once.

        Returns (final_revision, final_diff, persistence_status).
        """
        try:
            latest = self._case_service.get_latest_revision(case_id)
            new_prior_flags = (latest or {}).get("consistency_flags", []) or []
            new_revision = int((latest or {}).get("revision", record["revision"])) + 1
            new_diff = compute_revision_diff(new_prior_flags, current_flags) if new_revision >= 2 else {}
            retry_record = dict(record)
            retry_record["revision"] = new_revision

            self._case_service.insert_case_revision(retry_record)
            return new_revision, new_diff, "ok"
        except Exception:  # noqa: BLE001 — retry also failed: give up, best-effort
            return record["revision"], {}, "failed"

    @staticmethod
    def _build_record(
        case_id: str,
        revision: int,
        contract: dict[str, Any],
        documents: list[dict[str, Any]],
        flags: list[dict[str, Any]],
        lv3_result: dict[str, Any],
        claimant_key: str,
    ) -> dict[str, Any]:
        claimed_amount = None
        incident_type = ""
        injury_category = ""
        for doc in documents:
            fields = doc.get("fields", {})
            if fields.get("claimed_amount") is not None and claimed_amount is None:
                claimed_amount = float(fields["claimed_amount"])
            if fields.get("injury_name") and not injury_category:
                injury_category = fields["injury_name"]
            if fields.get("incident_description") and not incident_type:
                incident_type = fields.get("incident_description", "")[:64]

        coverage_item = ""
        if contract.get("coverage_items"):
            items = contract["coverage_items"]
            coverage_item = items[0] if isinstance(items, list) and items else str(items)

        # Coded values only — no document text, no free-text opinion (§6-3).
        consistency_flags = [
            {"kind": f.get("kind"), "flag_key": f.get("flag_key"), "verdict": None} for f in flags
        ]

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        return {
            "case_id": case_id,
            "revision": revision,
            "policy_type": contract.get("policy_type", ""),
            "coverage_item": coverage_item,
            "incident_type": incident_type,
            "injury_category": injury_category,
            "treatment_or_repair_period": None,
            "claimed_amount": claimed_amount,
            "consistency_flags": consistency_flags,
            "anomaly_score": lv3_result.get("anomaly_score"),
            "final_verdict": "needs_review",
            "assessor_edited": False,
            "claimant_key": claimant_key,
            "source_row_hash": None,
            "created_at": now,
            "updated_at": now,
        }
