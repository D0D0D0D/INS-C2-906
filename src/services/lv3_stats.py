"""AgentCore Platform v1.0 — INS-C2-906 Lv3 anomaly statistics (pure, no DB)"""

# docs/02_design.md §5-3 — Lv3 algorithm definition:
#   - Robust z-score: z = (x - median) / (1.4826 * MAD); MAD==0 -> IQR
#     fallback; IQR==0 too -> distribution degenerate / score invalid.
#   - Percentile: population-relative percentile of x.
#   - min_population_n: below this, no score is produced
#     (degraded: "insufficient_population").
#   - Repeat-claim pattern: distinct case_id count (not revision count).
#
# This module is intentionally DB-agnostic and independently unit-testable:
# callers pass in the already-queried population values (list[float]) and
# repeat-claim raw records; no database access happens here.

from __future__ import annotations

import statistics
from typing import Any

DEGRADED_INSUFFICIENT_POPULATION = "insufficient_population"
DEGRADED_DISTRIBUTION_DEGENERATE = "distribution_degenerate"
DEGRADED_DB_UNAVAILABLE = "db_unavailable"


def _median(values: list[float]) -> float:
    return statistics.median(values)


def _mad(values: list[float], med: float) -> float:
    deviations = [abs(v - med) for v in values]
    return statistics.median(deviations)


def _iqr(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    sorted_vals = sorted(values)
    q1 = statistics.median(sorted_vals[: len(sorted_vals) // 2])
    upper_half = sorted_vals[(len(sorted_vals) + 1) // 2 :]
    q3 = statistics.median(upper_half) if upper_half else q1
    return q3 - q1


def _percentile_rank(values: list[float], x: float) -> float:
    if not values:
        return 0.0
    n_le = sum(1 for v in values if v <= x)
    return round(100.0 * n_le / len(values), 2)


def compute_anomaly_score(
    population: list[float],
    x: float,
    min_population_n: int = 30,
) -> dict[str, Any]:
    """Compute the robust anomaly score for `x` against `population`.

    `population` must already exclude the current case_id's own revisions
    and be restricted to confirmed-only records for the matching
    policy_type x coverage_item slice (design §5-3) — this function does
    not know about case_id or DB schema; that filtering is the caller's
    responsibility (lv3_anomaly node / assessment_cases service).
    """
    population_n = len(population)
    if population_n < min_population_n:
        return {
            "anomaly_score": None,
            "percentile": None,
            "population_n": population_n,
            "median": None,
            "degraded": DEGRADED_INSUFFICIENT_POPULATION,
        }

    med = _median(population)
    mad = _mad(population, med)

    if mad > 0:
        z = (x - med) / (1.4826 * mad)
        return {
            "anomaly_score": round(z, 4),
            "percentile": _percentile_rank(population, x),
            "population_n": population_n,
            "median": med,
            "degraded": None,
        }

    iqr = _iqr(population)
    if iqr > 0:
        # IQR-based fallback: express deviation from median in IQR units
        # (design §5-3 "MAD=0のときはIQRベースへフォールバック")
        z = (x - med) / iqr
        return {
            "anomaly_score": round(z, 4),
            "percentile": _percentile_rank(population, x),
            "population_n": population_n,
            "median": med,
            "degraded": None,
        }

    return {
        "anomaly_score": None,
        "percentile": _percentile_rank(population, x),
        "population_n": population_n,
        "median": med,
        "degraded": DEGRADED_DISTRIBUTION_DEGENERATE,
    }


def compute_repeat_claim_pattern(
    claimant_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute repeat-claim frequency from raw records matching claimant_key.

    Args:
        claimant_records: rows with at least {"case_id": str}, already
            filtered by claimant_key and the lookback window by the caller.
            May include unconfirmed (needs_review/investigating) records —
            repeat-claim detection intentionally includes them (design §5-3:
            "反復照合のみ未確定レコードも対象に含める").

    Returns:
        {"distinct_case_count": int, "record_count": int}

    Counts DISTINCT case_id — revisions of the same case must not inflate
    the repeat-claim count (design §5-3 / §13-1).
    """
    case_ids = {r["case_id"] for r in claimant_records if r.get("case_id")}
    return {
        "distinct_case_count": len(case_ids),
        "record_count": len(claimant_records),
    }


def select_similar_cases(
    population_records: list[dict[str, Any]],
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Select up to `top_n` similar confirmed cases for reference display.

    `population_records` must already be filtered to the same population
    slice used for compute_anomaly_score (confirmed-only, matching
    policy_type x coverage_item, current case_id's revisions excluded).
    This function performs no filtering of its own — it is a pure top-N
    slice, keeping DB/query concerns out of the stats layer.
    """
    return population_records[:top_n]
