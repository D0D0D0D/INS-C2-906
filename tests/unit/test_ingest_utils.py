# INS-C2-906 — Unit tests: ingest row hashing (design §7-3)

from src.services.ingest_utils import compute_source_row_hash


class TestSourceRowHash:
    def test_deterministic(self):
        row = {"policy_type": "auto", "claimed_amount": 1000}
        assert compute_source_row_hash(row) == compute_source_row_hash(row)

    def test_key_order_independent(self):
        row1 = {"a": 1, "b": 2}
        row2 = {"b": 2, "a": 1}
        assert compute_source_row_hash(row1) == compute_source_row_hash(row2)

    def test_different_content_different_hash(self):
        row1 = {"a": 1}
        row2 = {"a": 2}
        assert compute_source_row_hash(row1) != compute_source_row_hash(row2)
