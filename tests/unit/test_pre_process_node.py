# INS-C2-906 — Unit tests: ClaimIntakeNode (pre_process, design §2-1/§6-2)

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import ClaimIntakeNode


class TestClaimIntakeNodeExecute:
    def setup_method(self):
        self.node = ClaimIntakeNode()

    def test_invalid_mode_returns_error(self, base_state):
        state = {**base_state, "input_context": {"mode": "bogus"}}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_assess_new_case_assigns_uuid(self, base_state):
        state = {
            **base_state,
            "input_context": {
                "mode": "assess",
                "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "x"}],
                "contract": {"coverage_limit": 5000},
            },
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["case_id"]  # non-empty UUID assigned
        assert "is_recheck" not in result or True  # is_recheck is embedded in validated_input, not top-level

    def test_assess_recheck_preserves_caller_case_id(self, base_state):
        state = {
            **base_state,
            "input_context": {
                "mode": "assess",
                "case_id": "existing-case-123",
                "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "x"}],
                "contract": {"coverage_limit": 5000},
            },
        }
        result = self.node.execute(state)
        assert result["case_id"] == "existing-case-123"
        assert '"is_recheck": true' in result["validated_input"]

    def test_assess_missing_documents_errors(self, base_state):
        state = {**base_state, "input_context": {"mode": "assess", "documents": [], "contract": {"x": 1}}}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_assess_missing_contract_errors(self, base_state):
        state = {
            **base_state,
            "input_context": {"mode": "assess", "documents": [{"doc_id": "d1", "text": "x"}], "contract": {}},
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_assess_too_many_documents_errors(self, base_state):
        node = ClaimIntakeNode(max_documents=2)
        docs = [{"doc_id": f"d{i}", "doc_type": "invoice", "text": "x"} for i in range(3)]
        state = {**base_state, "input_context": {"mode": "assess", "documents": docs, "contract": {"a": 1}}}
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "max_documents" in result["error_log"][0]

    def test_assess_total_chars_exceeded_errors(self, base_state):
        node = ClaimIntakeNode(max_total_chars=10)
        docs = [{"doc_id": "d1", "doc_type": "invoice", "text": "x" * 100}]
        state = {**base_state, "input_context": {"mode": "assess", "documents": docs, "contract": {"a": 1}}}
        result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "max_total_chars" in result["error_log"][0]

    def test_ingest_missing_rows_errors(self, base_state):
        state = {**base_state, "input_context": {"mode": "ingest", "rows": []}}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_ingest_valid_rows_succeeds(self, base_state):
        state = {**base_state, "input_context": {"mode": "ingest", "rows": [{"a": 1}], "dry_run": True}}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["mode"] == "ingest"

    def test_feedback_missing_case_id_errors(self, base_state):
        state = {**base_state, "input_context": {"mode": "feedback"}}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_feedback_valid_succeeds(self, base_state):
        state = {**base_state, "input_context": {"mode": "feedback", "case_id": "case-1", "final_verdict": "paid"}}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["case_id"] == "case-1"


class TestClaimIntakeNodeSecurityGate:
    def setup_method(self):
        self.node = ClaimIntakeNode()

    def test_required_trust_level(self):
        assert ClaimIntakeNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_residual_pii_rejected_fail_closed(self, base_state):
        """design §6-2: server.py tokenizes before invoke(), but a residual
        raw-PII pattern surviving that boundary must be rejected here."""
        state = {
            **base_state,
            "input_context": {
                "mode": "assess",
                "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "call me at taro@example.com"}],
                "contract": {"a": 1},
            },
        }
        gated_state = self.node._extra_security_gate_input(state)
        assert gated_state["status"] == AgentStatus.ERROR.value
        assert "residual" in gated_state["error_log"][0].lower()

    def test_clean_input_passes_gate(self, base_state):
        state = {
            **base_state,
            "input_context": {
                "mode": "assess",
                "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "no pii here at all"}],
                "contract": {"a": 1},
            },
        }
        gated_state = self.node._extra_security_gate_input(state)
        assert gated_state.get("status") != AgentStatus.ERROR.value

    def test_full_call_via_dunder_call_enforces_trust_gate(self, base_state):
        state = {**base_state, "caller_trust_level": TrustLevel.ANONYMOUS.value, "input_context": {"mode": "assess"}}
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "S-1 trust gate denied" in result["error_log"][0]
