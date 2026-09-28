"""AgentCore Platform v1.0 — INS-C2-906 inner: store_records (ingest)"""

# docs/02_design.md §3 / §7-3 — row-unit commit (never all-or-nothing).
# dry_run=true performs mapping/normalization preview only, no writes.
# Duplicate source_row_hash on reingest is skipped, not re-inserted
# (design §7-3).

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.assessment_case_service import AssessmentCaseService


class StoreRecordsNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, case_service: AssessmentCaseService | None = None) -> None:
        self._case_service = case_service

    def execute(self, state) -> dict:
        rows = state.get("ingest_rows", [])
        summary = dict(state.get("ingest_summary", {}))
        dry_run = bool(summary.get("dry_run", False))

        n_ok = 0
        n_skipped_duplicate = 0
        failures: list[dict] = list(summary.get("parse_errors", []))

        if not dry_run and self._case_service is not None:
            for i, row in enumerate(rows):
                try:
                    inserted = self._case_service.insert_ingest_row(row)
                    if inserted:
                        n_ok += 1
                    else:
                        n_skipped_duplicate += 1
                except Exception as e:  # noqa: BLE001 — row-unit failure, do not abort batch
                    failures.append({"row": i, "reason": str(e)})

        summary.update(
            {
                "n_ok": n_ok,
                "n_failed": len(failures),
                "n_skipped_duplicate": n_skipped_duplicate,
                "failures": failures,
                "dry_run": dry_run,
            }
        )

        emit_trace_event(
            "ingest_batch_completed",
            {"n_ok": n_ok, "n_failed": len(failures), "n_skipped_duplicate": n_skipped_duplicate, "dry_run": dry_run},
            state,
        )

        return {"ingest_summary": summary, "status": AgentStatus.SUCCESS.value}
