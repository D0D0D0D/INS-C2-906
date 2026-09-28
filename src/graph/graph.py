"""AgentCore Platform v1.0 — INS-C2-906 ClaimConsistencyCheckAgent outer graph"""

# docs/02_design.md §1 / §2-1 — Cat 2: fixed 5-node outer backbone
# (AgentBaseGraph, unmodified add_edges()) with domain complexity
# encapsulated in the `main` slot's GraphNode (ClaimWorkflowGraphNode),
# which wraps the inner ClaimCheckWorkflowGraph (src/graph/domain_workflow_graph.py).
#
#   START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                     ↓ (RETRY, max 3)
#                                  pre_process
#
# pre_process  = ClaimIntakeNode         (mode/schema validation, S-2 rescan)
# main         = ClaimWorkflowGraphNode  (GraphNode wrapping ClaimCheckWorkflowGraph)
# post_process = OpinionComposeNode      (Markdown + JSON opinion draft, S-3 hooks)

from framework.graph.agent_base_graph import AgentBaseGraph
from src.nodes.main_node import ClaimWorkflowGraphNode
from src.nodes.post_process_node import OpinionComposeNode
from src.nodes.pre_process_node import ClaimIntakeNode
from src.schemas.state import State


class ClaimConsistencyCheckAgent(AgentBaseGraph):
    """INS-C2-906 ClaimConsistencyCheckAgent — fixed-pipeline outer graph.

    Cat 2: the `main` slot holds a GraphNode wrapping the inner
    ClaimCheckWorkflowGraph (docs/02_design.md §3).
    """

    @property
    def name(self) -> str:
        return "ins_c2_906_claim_consistency_check_agent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode

        llm = self.config.get("llm")
        case_service = self.config.get("case_service")
        lv3_config = self.config.get("lv3", {})
        limits = self.config.get("limits", {})

        self._nodes["pre_process"] = ClaimIntakeNode(
            max_documents=limits.get("max_documents", 12),
            max_total_chars=limits.get("max_total_chars", 400_000),
        )
        self._nodes["main"] = ClaimWorkflowGraphNode(
            llm=llm, case_service=case_service, lv3_config=lv3_config
        )
        self._nodes["post_process"] = OpinionComposeNode()


# Backward-compatibility alias for older imports that reference `Graph`.
Graph = ClaimConsistencyCheckAgent
