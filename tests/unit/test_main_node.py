# INS-C2-906 — Unit tests: ClaimWorkflowGraphNode (main, GraphNode, design §3)

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import ClaimWorkflowGraphNode
from tests.conftest import assess_payload


class TestClaimWorkflowGraphNodeContract:
    def test_required_trust_level(self):
        assert ClaimWorkflowGraphNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_error_strategy_is_handle(self):
        """design §3/§8: graceful degradation, not fail-fast propagate."""
        assert ClaimWorkflowGraphNode.error_strategy == "handle"

    def test_propagate_hitl_is_false(self):
        assert ClaimWorkflowGraphNode.propagate_hitl is False

    def test_get_subgraph_is_cached_not_rebuilt(self, fake_llm, case_service):
        """Known bug pattern (prior Cat 2 templates): get_subgraph() must
        return the SAME instance across calls — never construct fresh."""
        node = ClaimWorkflowGraphNode(llm=fake_llm, case_service=case_service)
        first = node.get_subgraph()
        second = node.get_subgraph()
        assert first is second

    def test_extract_input_reads_validated_input(self, fake_llm, case_service):
        node = ClaimWorkflowGraphNode(llm=fake_llm, case_service=case_service)
        state = {"validated_input": '{"mode": "feedback"}'}
        assert node.extract_input(state) == '{"mode": "feedback"}'

    def test_merge_output_returns_only_changed_keys(self, fake_llm, case_service):
        node = ClaimWorkflowGraphNode(llm=fake_llm, case_service=case_service)
        sub_result = {
            "output": {"foo": "bar"},
            "status": "success",
            "lv1_flags": [],
            "lv2_flags": [],
            "lv3_result": {},
            "checked_items": [],
            "revision": 1,
            "revision_diff": {},
            "persistence_status": "ok",
            "case_id": "case-1",
            "ingest_summary": None,
            "feedback_result": None,
        }
        delta = node.merge_output({"case_id": "case-1"}, sub_result)
        assert set(delta.keys()) <= {
            "result",
            "status",
            "lv1_flags",
            "lv2_flags",
            "lv3_result",
            "checked_items",
            "revision",
            "revision_diff",
            "persistence_status",
            "case_id",
            "ingest_summary",
            "feedback_result",
        }
        assert delta["result"] == {"foo": "bar"}


class TestClaimWorkflowGraphNodeExecute:
    def test_full_call_executes_inner_graph(self, fake_llm, case_service, base_state):
        node = ClaimWorkflowGraphNode(llm=fake_llm, case_service=case_service)
        state = {**base_state, "validated_input": assess_payload()}
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_on_subgraph_error_returns_error_status(self, fake_llm, case_service, base_state):
        node = ClaimWorkflowGraphNode(llm=fake_llm, case_service=case_service)
        delta = node.on_subgraph_error(base_state, RuntimeError("boom"))
        assert delta["status"] == AgentStatus.ERROR.value
        assert "boom" in delta["error_log"][0]

    def test_unparseable_payload_degrades_gracefully_not_raise(self, fake_llm, case_service, base_state):
        """error_strategy=handle: an inner-graph parse failure must surface
        as status=error through the outer node call, not raise."""
        node = ClaimWorkflowGraphNode(llm=fake_llm, case_service=case_service)
        state = {**base_state, "validated_input": "not valid json{{{"}
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value
