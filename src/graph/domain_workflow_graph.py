"""AgentCore Platform v1.0 — INS-C2-906 ClaimCheckWorkflowGraph (inner graph)"""

# docs/02_design.md §3 — the inner Cat 2 domain workflow. Inherits BaseGraph
# directly (custom topology: 3-way mode router — not the fixed 5-node
# AgentBaseGraph backbone). Implements all 7 BaseGraph abstract methods.
#
#   START → parse_payload → mode_router ┬─(assess)──→ load_case_thread → structure_docs
#                                        │               → normalize_entities → lv1_deterministic
#                                        │               → lv2_cross_doc → lv3_anomaly
#                                        │               → persist_case → compose_findings → END
#                                        ├─(ingest)──→ parse_rows → map_columns
#                                        │               → minimize_normalize → store_records → END
#                                        └─(feedback)→ validate_case → apply_feedback → END
#
# CRITICAL (confirmed at runtime, design §3/§4-2): mode_router and route()
# MUST be annotated with ClaimWorkflowState — NOT AgentState. Annotating
# with AgentState causes LangGraph to project state down to AgentState's
# fields at the node boundary, silently dropping `mode` and other custom
# fields, causing the router to always fall through to a default branch.

from __future__ import annotations

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.inner.apply_feedback_node import ApplyFeedbackNode
from src.nodes.inner.compose_findings_node import ComposeFindingsNode
from src.nodes.inner.load_case_thread_node import LoadCaseThreadNode
from src.nodes.inner.lv1_deterministic_node import Lv1DeterministicNode
from src.nodes.inner.lv2_cross_doc_node import Lv2CrossDocNode
from src.nodes.inner.lv3_anomaly_node import Lv3AnomalyNode
from src.nodes.inner.map_columns_node import MapColumnsNode
from src.nodes.inner.minimize_normalize_node import MinimizeNormalizeNode
from src.nodes.inner.normalize_entities_node import NormalizeEntitiesNode
from src.nodes.inner.parse_payload_node import ParsePayloadNode
from src.nodes.inner.parse_rows_node import ParseRowsNode
from src.nodes.inner.persist_case_node import PersistCaseNode
from src.nodes.inner.store_records_node import StoreRecordsNode
from src.nodes.inner.structure_docs_node import StructureDocsNode
from src.nodes.inner.validate_case_node import ValidateCaseNode
from src.schemas.claim_workflow_state import ClaimWorkflowState


class ClaimCheckWorkflowGraph(BaseGraph):
    """Inner Cat 2 domain workflow — 3-way mode router (assess/ingest/feedback).

    Config keys (passed via ClaimWorkflowGraphNode._parent_config()):
        llm: BaseLLM instance (or None — Lv2/structure_docs/map_columns
             degrade gracefully per design §8 when absent/failing)
        case_service: AssessmentCaseService instance (or None — assess/ingest
             degrade / fail-fast per design §8, feedback errors without one)
        lv3: {min_population_n, similar_cases_top_n, repeat_lookback_days}
    """

    @property
    def name(self) -> str:
        return "claim_check_workflow"

    @property
    def state_schema(self) -> type:
        return ClaimWorkflowState

    def _validate_config(self) -> None:
        lv3_config = self.config.get("lv3", {})
        if lv3_config and not isinstance(lv3_config, dict):
            raise ConfigError(f"[{self.__class__.__name__}] 'lv3' config must be a dict, got: {lv3_config!r}")

    def register_nodes(self) -> None:
        # No super() call — BaseGraph.register_nodes() is abstract.
        llm = self.config.get("llm")
        case_service = self.config.get("case_service")
        lv3_config = self.config.get("lv3", {})

        self._nodes["parse_payload"] = ParsePayloadNode()

        # assess branch
        self._nodes["load_case_thread"] = LoadCaseThreadNode(case_service=case_service)
        self._nodes["structure_docs"] = StructureDocsNode(llm=llm)
        self._nodes["normalize_entities"] = NormalizeEntitiesNode()
        self._nodes["lv1_deterministic"] = Lv1DeterministicNode()
        self._nodes["lv2_cross_doc"] = Lv2CrossDocNode(llm=llm)
        self._nodes["lv3_anomaly"] = Lv3AnomalyNode(
            case_service=case_service,
            min_population_n=lv3_config.get("min_population_n", 30),
            similar_cases_top_n=lv3_config.get("similar_cases_top_n", 5),
            repeat_lookback_days=lv3_config.get("repeat_lookback_days", 365),
        )
        self._nodes["persist_case"] = PersistCaseNode(case_service=case_service)
        self._nodes["compose_findings"] = ComposeFindingsNode()

        # ingest branch
        self._nodes["parse_rows"] = ParseRowsNode()
        self._nodes["map_columns"] = MapColumnsNode(llm=llm)
        self._nodes["minimize_normalize"] = MinimizeNormalizeNode()
        self._nodes["store_records"] = StoreRecordsNode(case_service=case_service)

        # feedback branch
        self._nodes["validate_case"] = ValidateCaseNode(case_service=case_service)
        self._nodes["apply_feedback"] = ApplyFeedbackNode(case_service=case_service)

    def add_edges(self) -> None:
        self._sg.add_edge(START, "parse_payload")
        self._sg.add_conditional_edges(
            "parse_payload",
            self.mode_router,
            {
                "assess": "load_case_thread",
                "ingest": "parse_rows",
                "feedback": "validate_case",
                "error": END,
            },
        )

        # assess branch — linear once mode is confirmed; each node passes
        # through an upstream error via the same conditional-edge pattern
        # used at every step (design §3: GraphNode-level error propagation;
        # inner-node-level early exit uses AgentStatus.ERROR short-circuits
        # via the per-step router below).
        self._sg.add_conditional_edges(
            "load_case_thread", self._continue_or_end, {"continue": "structure_docs", "error": END}
        )
        self._sg.add_edge("structure_docs", "normalize_entities")
        self._sg.add_edge("normalize_entities", "lv1_deterministic")
        self._sg.add_edge("lv1_deterministic", "lv2_cross_doc")
        self._sg.add_edge("lv2_cross_doc", "lv3_anomaly")
        self._sg.add_edge("lv3_anomaly", "persist_case")
        self._sg.add_edge("persist_case", "compose_findings")
        self._sg.add_edge("compose_findings", END)

        # ingest branch
        self._sg.add_edge("parse_rows", "map_columns")
        self._sg.add_edge("map_columns", "minimize_normalize")
        self._sg.add_edge("minimize_normalize", "store_records")
        self._sg.add_edge("store_records", END)

        # feedback branch
        self._sg.add_conditional_edges(
            "validate_case", self._continue_or_end, {"continue": "apply_feedback", "error": END}
        )
        self._sg.add_edge("apply_feedback", END)

    def mode_router(self, state: ClaimWorkflowState) -> str:
        """3-way mode branch. MUST be annotated with ClaimWorkflowState (see
        module docstring) — AgentState annotation silently drops `mode`.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return "error"
        mode = state.get("mode", "")
        if mode in ("assess", "ingest", "feedback"):
            return mode
        return "error"

    def _continue_or_end(self, state: ClaimWorkflowState) -> str:
        """Early-exit router for assess/feedback branch nodes that can fail
        fast (unknown_case_id / unknown_revision / case_thread_unavailable).
        Annotated with ClaimWorkflowState for the same reason as mode_router.
        """
        return "error" if state.get("status") == AgentStatus.ERROR.value else "continue"

    def route(self, state: ClaimWorkflowState) -> str:
        """ABC 要件充足専用 — add_conditional_edges からは参照されない.

        Required by the BaseGraph ABC only. This graph's conditional edges
        are wired exclusively through mode_router / _continue_or_end (see
        add_edges()) and this method is never called at runtime.

        Do NOT pass this method to add_conditional_edges() — both branches
        return END by design, so
        wiring it in would silently terminate the graph on every path. If a
        new conditional edge is needed, add a purpose-built router (annotated
        with ClaimWorkflowState) instead of reusing this stub.
        """
        return END if state.get("status") == AgentStatus.ERROR.value else END

    def get_output(self, state: ClaimWorkflowState) -> dict:
        """Shape the sub_result dict handed to the outer
        ClaimWorkflowGraphNode.merge_output(). Designed together with that
        method (design §3).
        """
        return {
            "output": state.get("result"),
            "status": state.get("status"),
            "lv1_flags": state.get("lv1_flags", []),
            "lv2_flags": state.get("lv2_flags", []),
            "lv3_result": state.get("lv3_result", {}),
            "checked_items": state.get("checked_items", []),
            "revision": state.get("revision"),
            "revision_diff": state.get("revision_diff", {}),
            "persistence_status": state.get("persistence_status"),
            "case_id": state.get("case_id"),
            "ingest_summary": state.get("ingest_summary"),
            "feedback_result": state.get("feedback_result"),
            "error_log": state.get("error_log", []),
            "node_history": state.get("node_history", []),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
        }
