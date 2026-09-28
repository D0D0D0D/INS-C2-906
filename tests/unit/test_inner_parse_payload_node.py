# INS-C2-906 — Unit tests: ParsePayloadNode (inner, design §3)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.parse_payload_node import ParsePayloadNode
from tests.conftest import assess_payload


class TestParsePayloadNode:
    def setup_method(self):
        self.node = ParsePayloadNode()

    def test_assess_payload_deserializes(self, base_state):
        state = {**base_state, "user_input": assess_payload(case_id="", is_recheck=False)}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["mode"] == "assess"
        assert result["is_recheck"] is False
        assert "documents" in result
        assert "contract" in result

    def test_recheck_flag_preserved(self, base_state):
        state = {**base_state, "user_input": assess_payload(case_id="case-1", is_recheck=True)}
        result = self.node.execute(state)
        assert result["is_recheck"] is True
        assert result["case_id"] == "case-1"

    def test_invalid_json_errors(self, base_state):
        state = {**base_state, "user_input": "not json{{"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_missing_mode_errors(self, base_state):
        state = {**base_state, "user_input": '{"foo": "bar"}'}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_ingest_mode_deserializes_rows(self, base_state):
        import json

        state = {**base_state, "user_input": json.dumps({"mode": "ingest", "rows": [{"a": 1}], "dry_run": True})}
        result = self.node.execute(state)
        assert result["mode"] == "ingest"
        assert result["ingest_rows"] == [{"a": 1}]
        assert result["ingest_summary"]["dry_run"] is True

    def test_feedback_mode_deserializes(self, base_state):
        import json

        payload = {"mode": "feedback", "feedback_payload": {"case_id": "case-9", "final_verdict": "paid"}}
        state = {**base_state, "user_input": json.dumps(payload)}
        result = self.node.execute(state)
        assert result["mode"] == "feedback"
        assert result["case_id"] == "case-9"
