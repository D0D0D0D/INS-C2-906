# INS-C2-906 — Integration tests: full outer graph (compile + invoke)

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import ClaimConsistencyCheckAgent


def _make_agent(llm=None, case_service=None):
    agent = ClaimConsistencyCheckAgent(
        config={
            "max_retry": 1,
            "llm": llm,
            "case_service": case_service,
            "lv3": {"min_population_n": 30, "similar_cases_top_n": 5, "repeat_lookback_days": 365},
            "limits": {"max_documents": 12, "max_total_chars": 400_000},
        }
    )
    agent.compile()
    return agent


class TestOuterGraphAssessMode:
    def test_new_case_assess_success(self, fake_llm, case_service):
        agent = _make_agent(llm=fake_llm, case_service=case_service)
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {
            "mode": "assess",
            "case_id": "",
            "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "x"}],
            "contract": {"coverage_limit": 5000, "policy_type": "auto", "coverage_items": ["collision"]},
            "claimant_key": "claimant-1",
        }
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["output"]["mode"] == "assess"
        assert result["output"]["case_id"]

    def test_node_history_covers_full_pipeline(self, fake_llm, case_service):
        agent = _make_agent(llm=fake_llm, case_service=case_service)
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {
            "mode": "assess",
            "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "x"}],
            "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
        }
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        assert result["node_history"] == [
            "InitializeNode",
            "ClaimIntakeNode",
            "ClaimWorkflowGraphNode",
            "OpinionComposeNode",
            "FinalizeNode",
        ]

    def test_invalid_mode_routes_to_finalize_only(self, fake_llm, case_service):
        agent = _make_agent(llm=fake_llm, case_service=case_service)
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke("", ctx=ctx, input_context={"mode": "bogus"})
        assert result["status"] == AgentStatus.ERROR.value
        assert "OpinionComposeNode" not in result["node_history"]

    def test_recheck_increments_revision_end_to_end(self, fake_llm, case_service):
        agent = _make_agent(llm=fake_llm, case_service=case_service)
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        base_input = {
            "mode": "assess",
            "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "x"}],
            "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
        }
        result1 = agent.invoke("", ctx=ctx, input_context={**base_input, "case_id": ""})
        case_id = result1["output"]["case_id"]

        result2 = agent.invoke("", ctx=ctx, input_context={**base_input, "case_id": case_id})
        assert result2["output"]["revision"] == 2
        assert "revision_diff" in result2["output"]["findings"]


class TestOuterGraphIngestMode:
    def test_ingest_dry_run(self, fake_llm, case_service):
        agent = _make_agent(llm=fake_llm, case_service=case_service)
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(
            "", ctx=ctx, input_context={"mode": "ingest", "rows": [{"Policy Type": "auto"}], "dry_run": True}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["output"]["mode"] == "ingest"


class TestOuterGraphFeedbackMode:
    def test_feedback_unknown_case_id_errors(self, fake_llm, case_service):
        agent = _make_agent(llm=fake_llm, case_service=case_service)
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(
            "", ctx=ctx, input_context={"mode": "feedback", "case_id": "does-not-exist", "final_verdict": "paid"}
        )
        assert result["status"] == AgentStatus.ERROR.value


class TestOuterGraphSecurityInvariants:
    def test_anonymous_trust_level_blocks_pipeline(self, fake_llm, case_service):
        agent = _make_agent(llm=fake_llm, case_service=case_service)
        ctx = InvocationContext(caller_trust_level=TrustLevel.ANONYMOUS)
        result = agent.invoke(
            "",
            ctx=ctx,
            input_context={
                "mode": "assess",
                "documents": [{"doc_id": "d1", "doc_type": "invoice", "text": "x"}],
                "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
            },
        )
        assert result["status"] == AgentStatus.ERROR.value


class TestOuterGraphLlmExtractionReachesDeterministicChecks:
    """The LLM-extraction -> Lv1 wiring, end to end through the real graph.

    Every other integration test here sends ``text: "x"`` and the default
    ``FakeLLM``, whose ``{"flags": []}`` reply becomes the document's extracted
    ``fields``.  Nothing usable ever reaches Lv1, so those tests pass while the
    path from "what the model pulled out of the document" to "what the
    deterministic check ruled" is never exercised — a live run on 2026-08-20 was
    the first thing to cover it.

    The double here supplies only what a real model returns (per-document field
    extraction).  The verdict under assertion is computed by ``lv1_checks`` from
    those fields, never handed to it, and the fields are not planted on the
    documents by the test — they must travel StructureDocsNode ->
    NormalizeEntitiesNode -> Lv1DeterministicNode on their own.
    """

    _COVERAGE_LIMIT = 500_000
    _CLAIMED_TOTAL = 780_000

    def _extracting_llm(self, *, omit_claim_target: bool = False):
        """A double that answers each call the way a real model would.

        The raw shapes matter: a model returns the amount as it appeared in the
        document ("780,000") and the date as a timestamp, so
        NormalizeEntitiesNode has real work to do and cannot be a silent no-op.

        Dispatch is on the SYSTEM prompt, which names the call site. Keying on
        the user content instead misroutes: Lv2 is prompted with a rendering of
        the *extracted* documents, so its prompt also carries the accident
        wording and would be answered with the extraction schema — leaving the
        Lv2 stage silently empty while every assertion still passed.
        """
        import json

        from tests.conftest import FakeLLM

        unmatched: list[str] = []
        lv2_prompts: list[str] = []

        def respond(messages):
            system, user = messages[0]["content"], messages[-1]["content"]
            if "TEXTUAL inconsistencies" in system:
                lv2_prompts.append(user)
                return '{"flags": []}'
            if "extracting structured facts" not in system:
                unmatched.append(f"unknown system prompt ({len(system)} chars)")
                return "{}"
            if "追突" in user:
                return json.dumps(
                    {"incident_date": "2026-03-14T18:20:00", "incident_description": "追突により後部破損"},
                    ensure_ascii=False,
                )
            if "請求金額" in user:
                extracted = {"claimed_amount": "780,000"}
                if not omit_claim_target:
                    extracted["claim_target"] = "車両（リアバンパー）"
                return json.dumps(extracted, ensure_ascii=False)
            # Never echo the document body — a failure message travels to CI logs.
            unmatched.append(f"unrecognised document ({len(user)} chars)")
            return "{}"

        llm = FakeLLM(content_fn=respond)
        llm.unmatched = unmatched  # type: ignore[attr-defined]
        llm.lv2_prompts = lv2_prompts  # type: ignore[attr-defined]
        return llm

    def _run(self, case_service, **kwargs):
        self._llm = self._extracting_llm(**kwargs)
        agent = _make_agent(llm=self._llm, case_service=case_service)
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(
            "",
            ctx=ctx,
            input_context={
                "mode": "assess",
                "documents": [
                    {"doc_id": "d1", "doc_type": "accident_report", "text": "事故状況: 追突により後部破損"},
                    {"doc_id": "d3", "doc_type": "invoice", "text": "請求金額合計: 780,000円"},
                ],
                "contract": {
                    "coverage_limit": self._COVERAGE_LIMIT,
                    "deductible": 20_000,
                    "policy_start": "2026-01-01",
                    "policy_end": "2026-12-31",
                    "policy_type": "auto",
                    "coverage_items": ["collision"],
                },
                "claimant_key": "claimant-1",
            },
        )
        # Guards every test in this class, not just the first: a prompt the
        # double did not recognise means it answered the wrong call and the
        # assertions below would be measuring nothing.
        assert not self._llm.unmatched, f"the double did not recognise a prompt: {self._llm.unmatched}"
        assert len(self._llm.lv2_prompts) == 1, "Lv2 ran a number of times the test does not expect"
        return result

    def _check(self, result, name):
        items = result["output"]["findings"]["checked_items"]
        return next((item for item in items if item["check"] == name), None)

    def test_extracted_amount_drives_the_coverage_limit_verdict(self, case_service):
        result = self._run(case_service)

        assert result["status"] == AgentStatus.SUCCESS.value
        check = self._check(result, "amount_within_coverage_limit")
        assert check is not None, "Lv1 never saw an amount — extraction did not reach the check"
        assert check["passed"] is False
        # "780,000" -> 780000.0 proves NormalizeEntitiesNode ran on the way through
        assert check["detail"] == {
            "claimed_total": float(self._CLAIMED_TOTAL),
            "coverage_limit": self._COVERAGE_LIMIT,
        }

    def test_extracted_incident_date_drives_the_policy_period_verdict(self, case_service):
        result = self._run(case_service)

        check = self._check(result, "incident_within_policy_period")
        assert check is not None, "Lv1 never saw an incident date — extraction did not reach the check"
        assert check["passed"] is True
        assert check["detail"]["incident_date"] == "2026-03-14"

    def test_field_the_model_did_not_extract_is_named_as_missing(self, case_service):
        """The same wiring from the negative side.

        A real model often finds some fields and not others. When the invoice
        extraction omits ``claim_target``, that omission must survive the same
        three-node journey and be named per doc_type — proving the check reads
        what actually arrived rather than what the document happened to contain.
        """
        result = self._run(case_service, omit_claim_target=True)

        check = self._check(result, "required_fields_present[invoice]")
        assert check is not None
        assert check["passed"] is False
        assert check["detail"]["missing"] == ["claim_target"]
        # the field that *was* extracted still reached its own check
        assert self._check(result, "amount_within_coverage_limit")["passed"] is False

    def test_dates_reach_lv2_normalized_not_as_the_model_returned_them(self, case_service):
        """Isolates NormalizeEntitiesNode, which the Lv1 assertions cannot see.

        Lv1 re-parses dates itself (``_parse_iso_date`` truncates to 10 chars),
        so a pass-through normalizer still produces the right Lv1 verdict. The
        Lv2 prompt is built from the normalized documents and entities, so it is
        the one place where the normalizer's own effect is observable.
        """
        self._run(case_service)

        lv2_prompt = self._llm.lv2_prompts[0]
        assert "2026-03-14" in lv2_prompt
        assert "2026-03-14T18:20:00" not in lv2_prompt, (
            "the timestamp the model returned reached Lv2 unchanged — normalization did not run"
        )
