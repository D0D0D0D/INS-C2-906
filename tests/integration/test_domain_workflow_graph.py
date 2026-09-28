# INS-C2-906 — Integration tests: ClaimCheckWorkflowGraph (inner graph, design §3)
#
# Focused specifically on the mode_router state-annotation correctness —
# design §3/§4-2 confirms at runtime that annotating a conditional-branch
# function with AgentState (instead of the inner ClaimWorkflowState) causes
# LangGraph to project state down to AgentState's fields, silently dropping
# `mode` and mis-routing. These tests exercise the actual compiled graph
# (not a mocked router) so a regression to the wrong annotation is caught
# structurally (test_mode_router_annotation_is_not_agent_state) AND
# behaviorally (each branch actually reaches its expected node sequence).

import typing

from framework.schemas.agent_state import AgentState
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.domain_workflow_graph import ClaimCheckWorkflowGraph
from src.schemas.claim_workflow_state import ClaimWorkflowState
from tests.conftest import assess_payload

# graph.domain_workflow_graph.py uses `from __future__ import annotations`,
# so raw inspect.signature() returns string annotations — get_type_hints()
# resolves them to the actual class objects, which is what actually matters
# for this regression guard (a bare string mismatch would be a false pass).


def _ctx() -> InvocationContext:
    return InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def _make_graph(llm=None, case_service=None):
    graph = ClaimCheckWorkflowGraph(config={"llm": llm, "case_service": case_service, "lv3": {}})
    graph.compile()
    return graph


class TestModeRouterAnnotation:
    """Structural regression guard for the confirmed LangGraph projection bug."""

    def test_mode_router_annotated_with_claim_workflow_state_not_agent_state(self):
        hints = typing.get_type_hints(ClaimCheckWorkflowGraph.mode_router)
        assert hints["state"] is ClaimWorkflowState
        assert hints["state"] is not AgentState

    def test_continue_or_end_annotated_with_claim_workflow_state_not_agent_state(self):
        hints = typing.get_type_hints(ClaimCheckWorkflowGraph._continue_or_end)
        assert hints["state"] is ClaimWorkflowState
        assert hints["state"] is not AgentState


class TestModeRoutingBehavior:
    def test_assess_mode_reaches_compose_findings(self, fake_llm, case_service):
        graph = _make_graph(llm=fake_llm, case_service=case_service)
        ctx = _ctx()
        result = graph.invoke(assess_payload(), ctx=ctx)
        assert "ComposeFindingsNode" in result["node_history"]
        assert "StoreRecordsNode" not in result["node_history"]
        assert "ApplyFeedbackNode" not in result["node_history"]

    def test_ingest_mode_reaches_store_records(self, fake_llm, case_service):
        graph = _make_graph(llm=fake_llm, case_service=case_service)
        ctx = _ctx()
        import json

        payload = json.dumps({"mode": "ingest", "rows": [{"a": 1}], "dry_run": True})
        result = graph.invoke(payload, ctx=ctx)
        assert "StoreRecordsNode" in result["node_history"]
        assert "ComposeFindingsNode" not in result["node_history"]
        assert "ApplyFeedbackNode" not in result["node_history"]

    def test_feedback_mode_reaches_apply_feedback(self, fake_llm, case_service):
        case_service.seed({"case_id": "case-1", "revision": 1, "final_verdict": "needs_review"})
        graph = _make_graph(llm=fake_llm, case_service=case_service)
        ctx = _ctx()
        import json

        payload = json.dumps({"mode": "feedback", "feedback_payload": {"case_id": "case-1", "final_verdict": "paid"}})
        result = graph.invoke(payload, ctx=ctx)
        assert "ApplyFeedbackNode" in result["node_history"]
        assert "ComposeFindingsNode" not in result["node_history"]
        assert "StoreRecordsNode" not in result["node_history"]

    def test_unknown_mode_routes_to_error_end(self, fake_llm, case_service):
        graph = _make_graph(llm=fake_llm, case_service=case_service)
        ctx = _ctx()
        import json

        payload = json.dumps({"mode": "not-a-real-mode"})
        result = graph.invoke(payload, ctx=ctx)
        assert result["status"] == "error"
        assert "ComposeFindingsNode" not in result["node_history"]

    def test_feedback_unknown_case_id_early_exits_before_apply_feedback(self, fake_llm, case_service):
        """_continue_or_end router: validate_case error short-circuits to END
        without reaching apply_feedback."""
        graph = _make_graph(llm=fake_llm, case_service=case_service)
        ctx = _ctx()
        import json

        payload = json.dumps({"mode": "feedback", "feedback_payload": {"case_id": "does-not-exist"}})
        result = graph.invoke(payload, ctx=ctx)
        assert result["status"] == "error"
        assert "ApplyFeedbackNode" not in result["node_history"]
