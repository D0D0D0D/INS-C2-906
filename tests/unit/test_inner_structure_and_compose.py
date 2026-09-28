# INS-C2-906 — Unit tests: StructureDocsNode, NormalizeEntitiesNode,
# Lv1DeterministicNode wiring, ComposeFindingsNode (design §3/§5-1)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.compose_findings_node import ComposeFindingsNode
from src.nodes.inner.normalize_entities_node import NormalizeEntitiesNode
from src.nodes.inner.structure_docs_node import StructureDocsNode
from tests.conftest import FailingLLM, FakeLLM


class TestStructureDocsNode:
    def test_no_llm_leaves_fields_empty(self, base_state):
        node = StructureDocsNode(llm=None)
        state = {**base_state, "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "x"}]}
        result = node.execute(state)
        assert result["documents"][0]["fields"] == {}
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_llm_failure_degrades_single_doc_not_whole_batch(self, base_state):
        """design §8: LLM failure for one doc must not abort the whole assess run."""
        node = StructureDocsNode(llm=FailingLLM())
        state = {
            **base_state,
            "documents": [
                {"doc_id": "d1", "doc_type": "invoice", "text": "x"},
                {"doc_id": "d2", "doc_type": "accident_report", "text": "y"},
            ],
        }
        result = node.execute(state)
        assert len(result["documents"]) == 2
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "error_log" in result

    def test_llm_success_populates_fields(self, base_state):
        node = StructureDocsNode(llm=FakeLLM(content='{"claimed_amount": 1000}'))
        state = {**base_state, "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "x"}]}
        result = node.execute(state)
        assert result["documents"][0]["fields"]["claimed_amount"] == 1000

    def test_one_llm_call_per_document_not_mixed(self, base_state):
        """design §3: 1 document = 1 LLM call unit — documents never mixed in a single prompt."""
        llm = FakeLLM(content='{"claimed_amount": 1000}')
        node = StructureDocsNode(llm=llm)
        state = {
            **base_state,
            "documents": [
                {"doc_id": "d1", "doc_type": "invoice", "text": "doc one text"},
                {"doc_id": "d2", "doc_type": "medical_certificate", "text": "doc two text"},
            ],
        }
        node.execute(state)
        assert len(llm.calls) == 2


class TestNormalizeEntitiesNode:
    def test_normalizes_amount_and_date(self, base_state):
        node = NormalizeEntitiesNode()
        state = {
            **base_state,
            "documents": [
                {
                    "doc_id": "d1",
                    "doc_type": "invoice",
                    "fields": {"claimed_amount": "1,000", "incident_date": "2026-06-01T00:00:00"},
                }
            ],
        }
        result = node.execute(state)
        fields = result["documents"][0]["fields"]
        assert fields["claimed_amount"] == 1000.0
        assert fields["incident_date"] == "2026-06-01"
        assert len(result["entities"]) == 2


class TestComposeFindingsNode:
    def test_integrates_all_levels_and_diff(self, base_state):
        node = ComposeFindingsNode()
        state = {
            **base_state,
            "case_id": "case-1",
            "revision": 2,
            "revision_diff": {"counts": {"resolved": 1, "new": 0, "continuing": 1}},
            "lv1_flags": [{"kind": "X"}],
            "lv2_flags": [],
            "lv3_result": {"degraded": None},
            "checked_items": [{"check": "y", "passed": True}],
            "persistence_status": "ok",
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        findings = result["result"]
        assert findings["case_id"] == "case-1"
        assert findings["revision"] == 2
        assert findings["revision_diff"]["counts"]["resolved"] == 1
        assert findings["lv1_flags"] == [{"kind": "X"}]

    def test_does_not_recompute_diff_display_only(self, base_state):
        """design §3: compose_findings only formats — the diff itself is
        already confirmed by persist_case; this node must pass it through
        unmodified."""
        node = ComposeFindingsNode()
        original_diff = {"counts": {"resolved": 3, "new": 2, "continuing": 1}}
        state = {**base_state, "revision_diff": original_diff}
        result = node.execute(state)
        assert result["result"]["revision_diff"] == original_diff
