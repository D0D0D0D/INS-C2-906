# INS-C2-906 — Unit tests: Lv2CrossDocNode (inner, design §5-2/§8)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.lv2_cross_doc_node import Lv2CrossDocNode
from tests.conftest import FailingLLM, FakeLLM


class TestLv2CrossDocNode:
    def test_no_llm_configured_skips_gracefully(self, base_state):
        node = Lv2CrossDocNode(llm=None)
        state = {**base_state, "documents": [], "entities": []}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["lv2_flags"] == []
        assert "error_log" in result

    def test_llm_failure_degrades_gracefully(self, base_state):
        """design §8: LLM call failure -> Lv2 skipped, Lv1-only findings."""
        node = Lv2CrossDocNode(llm=FailingLLM())
        state = {**base_state, "documents": [], "entities": []}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["lv2_flags"] == []

    def test_valid_flag_accepted(self, base_state):
        content = (
            '{"flags": [{"kind": "LV2_DATE_CONFLICT", '
            '"anchors": ["accident_report.incident_date", "medical_cert.first_visit_date"], '
            '"evidence": [{"doc_id": "d1", "quote": "2026-01-01", "field": "incident_date"}, '
            '{"doc_id": "d2", "quote": "2026-02-01", "field": "first_visit_date"}], '
            '"reason": "dates conflict", "severity": "medium"}]}'
        )
        node = Lv2CrossDocNode(llm=FakeLLM(content=content))
        state = {**base_state, "documents": [], "entities": []}
        result = node.execute(state)
        assert len(result["lv2_flags"]) == 1
        assert result["lv2_flags"][0]["kind"] == "LV2_DATE_CONFLICT"
        assert "flag_key" in result["lv2_flags"][0]

    def test_out_of_enum_kind_discarded(self, base_state):
        """design §5-2: LLM-proposed kind outside the fixed taxonomy is discarded."""
        content = (
            '{"flags": [{"kind": "MADE_UP_KIND", "anchors": ["a.x", "b.y"], '
            '"evidence": [{"doc_id": "d1", "quote": "x", "field": "f"}], '
            '"reason": "x", "severity": "low"}]}'
        )
        node = Lv2CrossDocNode(llm=FakeLLM(content=content))
        state = {**base_state, "documents": [], "entities": []}
        result = node.execute(state)
        assert result["lv2_flags"] == []

    def test_evidence_less_flag_discarded(self, base_state):
        """design §5-2: flags without evidence are discarded before compose."""
        content = (
            '{"flags": [{"kind": "LV2_DATE_CONFLICT", "anchors": ["a.x", "b.y"], '
            '"evidence": [], "reason": "x", "severity": "low"}]}'
        )
        node = Lv2CrossDocNode(llm=FakeLLM(content=content))
        state = {**base_state, "documents": [], "entities": []}
        result = node.execute(state)
        assert result["lv2_flags"] == []

    def test_flag_key_stable_across_reruns_with_different_quote_wording(self, base_state):
        """design §5-2/§13-1: same logical contradiction re-detected after
        document resend with different quote wording must yield the same
        flag_key (anchors identical, only quote text differs)."""

        def make_content(quote_text: str) -> str:
            return (
                '{"flags": [{"kind": "LV2_DATE_CONFLICT", '
                '"anchors": ["accident_report.incident_date", "medical_cert.first_visit_date"], '
                f'"evidence": [{{"doc_id": "d1", "quote": "{quote_text}", "field": "incident_date"}}], '
                '"reason": "dates conflict", "severity": "medium"}]}'
            )

        node1 = Lv2CrossDocNode(llm=FakeLLM(content=make_content("original wording")))
        node2 = Lv2CrossDocNode(llm=FakeLLM(content=make_content("completely different wording")))
        state = {**base_state, "documents": [], "entities": []}

        result1 = node1.execute(state)
        result2 = node2.execute(state)

        assert result1["lv2_flags"][0]["flag_key"] == result2["lv2_flags"][0]["flag_key"]

    def test_flag_key_stable_regardless_of_anchor_order_from_llm(self, base_state):
        """design §5-2/§13-1: document order should not change flag_key —
        exercised here via anchor order returned by the LLM."""

        def make_content(anchors: list[str]) -> str:
            import json

            return json.dumps(
                {
                    "flags": [
                        {
                            "kind": "LV2_DATE_CONFLICT",
                            "anchors": anchors,
                            "evidence": [{"doc_id": "d1", "quote": "x", "field": "incident_date"}],
                            "reason": "dates conflict",
                            "severity": "medium",
                        }
                    ]
                }
            )

        node1 = Lv2CrossDocNode(
            llm=FakeLLM(content=make_content(["accident_report.incident_date", "medical_cert.first_visit_date"]))
        )
        node2 = Lv2CrossDocNode(
            llm=FakeLLM(content=make_content(["medical_cert.first_visit_date", "accident_report.incident_date"]))
        )
        state = {**base_state, "documents": [], "entities": []}

        result1 = node1.execute(state)
        result2 = node2.execute(state)

        assert result1["lv2_flags"][0]["flag_key"] == result2["lv2_flags"][0]["flag_key"]
