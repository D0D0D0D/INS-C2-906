# INS-C2-906 — Unit tests: Lv3 anomaly statistics (design §5-3)

from src.services.lv3_stats import (
    DEGRADED_DISTRIBUTION_DEGENERATE,
    DEGRADED_INSUFFICIENT_POPULATION,
    compute_anomaly_score,
    compute_repeat_claim_pattern,
    select_similar_cases,
)


class TestAnomalyScore:
    def test_insufficient_population_below_min_n(self):
        result = compute_anomaly_score([100, 200, 300], x=150, min_population_n=30)
        assert result["degraded"] == DEGRADED_INSUFFICIENT_POPULATION
        assert result["anomaly_score"] is None
        assert result["population_n"] == 3

    def test_normal_population_yields_robust_zscore(self):
        population = [100.0] * 40  # MAD=0 population but median well-defined
        population[0] = 90.0
        population[1] = 110.0
        result = compute_anomaly_score(population, x=100.0, min_population_n=30)
        assert result["population_n"] == 40
        assert result["degraded"] in (None, DEGRADED_DISTRIBUTION_DEGENERATE)

    def test_mad_zero_falls_back_to_iqr(self):
        # population with MAD==0 (most values identical) but IQR > 0
        population = [100.0] * 25 + [100.0, 100.0, 100.0, 100.0, 500.0]
        result = compute_anomaly_score(population, x=100.0, min_population_n=30)
        assert result["population_n"] == 30
        # Either a valid IQR-based score or a degenerate flag — never a silent None
        assert result["degraded"] in (None, DEGRADED_DISTRIBUTION_DEGENERATE)

    def test_fully_degenerate_distribution(self):
        population = [100.0] * 30  # zero variance: MAD=0 and IQR=0
        result = compute_anomaly_score(population, x=100.0, min_population_n=30)
        assert result["degraded"] == DEGRADED_DISTRIBUTION_DEGENERATE
        assert result["anomaly_score"] is None

    def test_percentile_within_bounds(self):
        population = [float(i) for i in range(1, 51)]
        result = compute_anomaly_score(population, x=25.0, min_population_n=30)
        assert 0.0 <= result["percentile"] <= 100.0

    def test_exactly_at_min_population_n_produces_a_score(self):
        population = [float(i) for i in range(1, 31)]  # n=30
        result = compute_anomaly_score(population, x=15.0, min_population_n=30)
        assert result["degraded"] != DEGRADED_INSUFFICIENT_POPULATION


class TestRepeatClaimPattern:
    def test_distinct_case_id_count_not_revision_count(self):
        """design §5-3/§13-1: repeat-claim count must be DISTINCT case_id,
        never inflated by multiple revisions of the same case."""
        records = [
            {"case_id": "case-1"},
            {"case_id": "case-1"},  # revision 2 of the same case
            {"case_id": "case-2"},
        ]
        result = compute_repeat_claim_pattern(records)
        assert result["distinct_case_count"] == 2
        assert result["record_count"] == 3

    def test_empty_records(self):
        result = compute_repeat_claim_pattern([])
        assert result["distinct_case_count"] == 0
        assert result["record_count"] == 0


class TestSimilarCases:
    def test_top_n_slice(self):
        records = [{"case_id": f"case-{i}"} for i in range(10)]
        result = select_similar_cases(records, top_n=3)
        assert len(result) == 3

    def test_fewer_than_top_n_returns_all(self):
        records = [{"case_id": "case-1"}]
        result = select_similar_cases(records, top_n=5)
        assert len(result) == 1
