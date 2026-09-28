"""AgentCore Platform v1.0 — INS-C2-906 inner: lv3_anomaly"""

# docs/02_design.md §5-3 / §7-1 (2) / §8 — anomaly scoring against the
# assessment_cases population, run BEFORE persist_case (current claim is
# never in its own population — design §7-1 (2)). Excludes ALL revisions of
# the current case_id from both population and similar-cases (self-
# containment bias avoidance, design §5-3 extension). On DB-unreachable for
# a NEW case, degrades gracefully (design §8: "Lv3 スキップ
# degraded: db_unavailable"); a DB-unreachable during a RECHECK is instead
# handled fail-fast by load_case_thread (§2-3/§8) before this node ever runs.

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.assessment_case_service import AssessmentCaseService
from src.services.lv3_stats import (
    DEGRADED_DB_UNAVAILABLE,
    compute_anomaly_score,
    compute_repeat_claim_pattern,
    select_similar_cases,
)


class Lv3AnomalyNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        case_service: AssessmentCaseService | None = None,
        min_population_n: int = 30,
        similar_cases_top_n: int = 5,
        repeat_lookback_days: int = 365,
    ) -> None:
        self._case_service = case_service
        self._min_population_n = min_population_n
        self._similar_cases_top_n = similar_cases_top_n
        self._repeat_lookback_days = repeat_lookback_days

    def execute(self, state) -> dict:
        contract = state.get("contract", {})
        documents = state.get("documents", [])
        case_id = state.get("case_id", "")
        claimant_key = state.get("claimant_key", "")

        policy_type = contract.get("policy_type", "")
        coverage_item = ""
        if contract.get("coverage_items"):
            coverage_item = contract["coverage_items"][0] if isinstance(contract["coverage_items"], list) else str(
                contract["coverage_items"]
            )

        claimed_amount = None
        for doc in documents:
            fields = doc.get("fields", {})
            if fields.get("claimed_amount") is not None:
                claimed_amount = float(fields["claimed_amount"])
                break

        if self._case_service is None:
            emit_trace_event("lv3_anomaly_scored", {"degraded": DEGRADED_DB_UNAVAILABLE}, state)
            return {
                "lv3_result": {
                    "anomaly_score": None,
                    "percentile": None,
                    "population_n": 0,
                    "similar_cases": [],
                    "repeat_claim": None,
                    "degraded": DEGRADED_DB_UNAVAILABLE,
                },
                "status": AgentStatus.SUCCESS.value,
            }

        try:
            population_records = self._case_service.query_population(
                policy_type, coverage_item, exclude_case_id=case_id
            )
        except Exception as e:  # noqa: BLE001 — DB unreachable, degrade (new case only; recheck fails earlier)
            emit_trace_event("lv3_anomaly_scored", {"degraded": DEGRADED_DB_UNAVAILABLE, "error": str(e)}, state)
            return {
                "lv3_result": {
                    "anomaly_score": None,
                    "percentile": None,
                    "population_n": 0,
                    "similar_cases": [],
                    "repeat_claim": None,
                    "degraded": DEGRADED_DB_UNAVAILABLE,
                },
                "status": AgentStatus.SUCCESS.value,
            }

        population_amounts = [
            float(r["claimed_amount"]) for r in population_records if r.get("claimed_amount") is not None
        ]

        anomaly: dict[str, Any] = {
            "anomaly_score": None,
            "percentile": None,
            "population_n": len(population_records),
            "median": None,
            "degraded": "insufficient_population",
        }
        if claimed_amount is not None:
            anomaly = compute_anomaly_score(population_amounts, claimed_amount, self._min_population_n)

        similar_cases = select_similar_cases(population_records, self._similar_cases_top_n)

        repeat_claim = None
        if claimant_key:
            try:
                claimant_records = self._case_service.query_claimant_records(
                    claimant_key, self._repeat_lookback_days
                )
                # distinct case_id count only — exclude the current case_id's
                # own (possibly already-persisted-in-a-prior-attempt) rows
                claimant_records = [r for r in claimant_records if r.get("case_id") != case_id]
                repeat_claim = compute_repeat_claim_pattern(claimant_records)
            except Exception:  # noqa: BLE001 — repeat-claim is best-effort
                repeat_claim = None

        lv3_result = {
            "anomaly_score": anomaly.get("anomaly_score"),
            "percentile": anomaly.get("percentile"),
            "population_n": anomaly.get("population_n"),
            "median": anomaly.get("median"),
            "similar_cases": similar_cases,
            "repeat_claim": repeat_claim,
            "degraded": anomaly.get("degraded"),
        }

        emit_trace_event(
            "lv3_anomaly_scored",
            {"population_n": lv3_result["population_n"], "degraded": lv3_result["degraded"]},
            state,
        )

        return {"lv3_result": lv3_result, "status": AgentStatus.SUCCESS.value}
