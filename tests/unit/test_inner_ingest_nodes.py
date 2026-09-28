# INS-C2-906 — Unit tests: ingest branch inner nodes (design §7-3/§13-1)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.map_columns_node import MapColumnsNode
from src.nodes.inner.minimize_normalize_node import MinimizeNormalizeNode
from src.nodes.inner.parse_rows_node import ParseRowsNode
from src.nodes.inner.store_records_node import StoreRecordsNode


class TestParseRowsNode:
    def test_valid_rows_pass_through(self, base_state):
        node = ParseRowsNode()
        state = {**base_state, "ingest_rows": [{"a": 1}, {"b": 2}]}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(result["ingest_rows"]) == 2

    def test_non_dict_row_recorded_as_parse_error_not_aborting(self, base_state):
        """design §7-3: row-unit failure must not abort the whole batch."""
        node = ParseRowsNode()
        state = {**base_state, "ingest_rows": [{"a": 1}, "not-a-dict", {"b": 2}]}
        result = node.execute(state)
        assert len(result["ingest_rows"]) == 2
        assert len(result["ingest_summary"]["parse_errors"]) == 1


class TestMapColumnsNode:
    def test_heuristic_mapping_without_llm(self, base_state):
        node = MapColumnsNode(llm=None)
        state = {**base_state, "ingest_rows": [{"Policy Type": "auto", "Claimed Amount": "1000"}]}
        result = node.execute(state)
        mapping = result["ingest_summary"]["column_mapping"]
        assert mapping.get("Policy Type") == "policy_type"

    def test_mapping_always_in_response(self, base_state, fake_llm):
        node = MapColumnsNode(llm=fake_llm)
        # fake_llm returns {"flags": []} by default (not a mapping shape) —
        # exercise the .get("mapping", {}) fallback path.
        state = {**base_state, "ingest_rows": [{"col_a": "x"}]}
        result = node.execute(state)
        assert "column_mapping" in result["ingest_summary"]


class TestMinimizeNormalizeNode:
    def test_projects_only_mapped_columns(self, base_state):
        node = MinimizeNormalizeNode()
        state = {
            **base_state,
            "ingest_rows": [{"Policy Type": "auto", "Unmapped Column": "secret free text"}],
            "ingest_summary": {"column_mapping": {"Policy Type": "policy_type", "Unmapped Column": None}},
        }
        result = node.execute(state)
        row = result["ingest_rows"][0]
        assert row["policy_type"] == "auto"
        assert "Unmapped Column" not in row
        assert "secret free text" not in row.values()

    def test_source_row_hash_added(self, base_state):
        node = MinimizeNormalizeNode()
        state = {
            **base_state,
            "ingest_rows": [{"Policy Type": "auto"}],
            "ingest_summary": {"column_mapping": {"Policy Type": "policy_type"}},
        }
        result = node.execute(state)
        assert "source_row_hash" in result["ingest_rows"][0]


class TestStoreRecordsNode:
    def test_dry_run_performs_no_writes(self, base_state, case_service):
        node = StoreRecordsNode(case_service=case_service)
        state = {
            **base_state,
            "ingest_rows": [{"policy_type": "auto", "source_row_hash": "h1"}],
            "ingest_summary": {"dry_run": True},
        }
        result = node.execute(state)
        assert result["ingest_summary"]["n_ok"] == 0
        assert case_service.rows == {}

    def test_row_unit_commit_success(self, base_state, case_service):
        node = StoreRecordsNode(case_service=case_service)
        state = {
            **base_state,
            "ingest_rows": [
                {"case_id": "hist-1", "policy_type": "auto", "source_row_hash": "h1"},
                {"case_id": "hist-2", "policy_type": "auto", "source_row_hash": "h2"},
            ],
            "ingest_summary": {"dry_run": False},
        }
        result = node.execute(state)
        assert result["ingest_summary"]["n_ok"] == 2
        assert result["ingest_summary"]["n_failed"] == 0

    def test_duplicate_hash_skipped_not_reinserted(self, base_state, case_service):
        """design §7-3: reingest of identical source_row_hash is skipped."""
        node = StoreRecordsNode(case_service=case_service)
        row = {"case_id": "hist-1", "policy_type": "auto", "source_row_hash": "dup-hash"}
        state = {**base_state, "ingest_rows": [row], "ingest_summary": {"dry_run": False}}

        node.execute(state)
        result2 = node.execute(state)  # re-ingest same row

        assert result2["ingest_summary"]["n_skipped_duplicate"] == 1
        assert result2["ingest_summary"]["n_ok"] == 0
