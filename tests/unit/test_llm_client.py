# INS-C2-906 — Unit tests: src/services/llm_client.call_structured

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import ClaimConsistencyCheckAgent
from src.services.llm_client import LLMInvocationError, call_structured
from tests.conftest import FakeLLM


class TestCallStructuredResponseShape:
    """Every caller reads the result as a mapping, so the seam must return one."""

    def test_json_object_is_returned(self):
        assert call_structured(FakeLLM(content='{"a": 1}'), "s", "u") == {"a": 1}

    @pytest.mark.parametrize(
        "content",
        [
            "[]",
            '[{"flags": []}]',
            # this one used to survive: dict([["claimed_amount", 1000]]) succeeds,
            # so a bare array of pairs silently became extracted fields
            '[["claimed_amount", 1000]]',
            '"just a string"',
            "42",
            "null",
            "true",
        ],
    )
    def test_valid_json_that_is_not_an_object_is_refused(self, content):
        """A model answering with a bare array or scalar is still valid JSON.

        Letting it through hands a non-mapping to callers that immediately call
        ``.get()``, which raises outside the ``LLMInvocationError`` degradation
        path every caller is written around.
        """
        with pytest.raises(LLMInvocationError):
            call_structured(FakeLLM(content=content), "s", "u")

    def test_refusal_reports_the_type_only_and_never_the_body(self):
        secret = '["RAW-DOCUMENT-MARKER"]'
        with pytest.raises(LLMInvocationError) as excinfo:
            call_structured(FakeLLM(content=secret), "s", "u")
        assert "list" in str(excinfo.value)
        assert "RAW-DOCUMENT-MARKER" not in str(excinfo.value)

    def test_non_json_content_is_refused(self):
        with pytest.raises(LLMInvocationError):
            call_structured(FakeLLM(content="I cannot answer that."), "s", "u")

    def test_empty_content_is_refused(self):
        with pytest.raises(LLMInvocationError):
            call_structured(FakeLLM(content=""), "s", "u")


class TestNonObjectResponseDegradesInsteadOfCrashing:
    def test_array_from_lv2_only_degrades_that_stage_not_the_run(self, case_service):
        """End to end: the array must land in the documented Lv2 degradation.

        Design §8 says an LLM failure degrades to Lv1-only findings. Before the
        seam checked the parsed type, this exact response escaped that path and
        raised ``AttributeError`` instead.

        Only the Lv2 call is given the bad response — structuring returns a
        usable object. Feeding ``[]`` to both stages would leave extraction empty
        too, and then ``lv2_flags == []`` would hold even if Lv2 never ran at all.
        """
        import json

        def respond(messages):
            if "TEXTUAL inconsistencies" in messages[0]["content"]:
                return "[]"
            return json.dumps({"claimed_amount": "780,000", "claim_target": "車両"}, ensure_ascii=False)

        agent = ClaimConsistencyCheckAgent(
            config={
                "max_retry": 1,
                "llm": FakeLLM(content_fn=respond),
                "case_service": case_service,
                "lv3": {"min_population_n": 30, "similar_cases_top_n": 5, "repeat_lookback_days": 365},
                "limits": {"max_documents": 12, "max_total_chars": 400_000},
            }
        )
        agent.compile()
        result = agent.invoke(
            "",
            ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
            input_context={
                "mode": "assess",
                "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "請求金額合計: 780,000円"}],
                "contract": {"coverage_limit": 500_000, "policy_type": "auto", "coverage_items": ["collision"]},
                "claimant_key": "claimant-1",
            },
        )

        assert result["status"] == AgentStatus.SUCCESS.value
        findings = result["output"]["findings"]
        # Lv2 degraded ...
        assert findings["lv2_flags"] == []
        # ... while Lv1 still ruled on what structuring did extract, which is what
        # "degrade to Lv1-only findings" has to mean.
        amount = next(i for i in findings["checked_items"] if i["check"] == "amount_within_coverage_limit")
        assert amount["passed"] is False
        assert amount["detail"] == {"claimed_total": 780000.0, "coverage_limit": 500_000}
