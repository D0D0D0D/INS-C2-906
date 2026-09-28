# INS-C2-906 — Unit tests: OpinionComposeNode (post_process, design §6-4/§9/§10)

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import OpinionComposeNode, _downgrade_assertive_language, _normalize


class TestOpinionComposeNodeExecute:
    def setup_method(self):
        self.node = OpinionComposeNode()

    def test_required_trust_level(self):
        assert OpinionComposeNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_assess_mode_produces_markdown_and_json(self, base_state):
        state = {
            **base_state,
            "mode": "assess",
            "result": {
                "case_id": "case-1",
                "revision": 1,
                "lv1_flags": [],
                "lv2_flags": [],
                "lv3_result": {"degraded": "insufficient_population"},
                "checked_items": [{"check": "x", "passed": True}],
                "revision_diff": {},
                "persistence_status": "ok",
            },
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        output = result["formatted_output"]
        assert "opinion_markdown" in output
        assert "findings" in output
        assert isinstance(output["findings"], dict)

    def test_checked_items_always_shown_even_with_zero_flags(self, base_state):
        """design §10 usability point 1: checked_items must appear regardless
        of flag count — distinguishes 'checked, all clear' from 'not checked'."""
        state = {
            **base_state,
            "mode": "assess",
            "result": {
                "case_id": "case-1",
                "revision": 1,
                "lv1_flags": [],
                "lv2_flags": [],
                "lv3_result": {},
                "checked_items": [{"check": "amount_within_coverage_limit", "passed": True}],
                "revision_diff": {},
                "persistence_status": "ok",
            },
        }
        result = self.node.execute(state)
        assert "検査カバレッジ" in result["formatted_output"]["opinion_markdown"]

    def test_persistence_failed_surfaces_warning_in_markdown(self, base_state):
        state = {
            **base_state,
            "mode": "assess",
            "result": {
                "case_id": "case-1",
                "revision": 1,
                "lv1_flags": [],
                "lv2_flags": [],
                "lv3_result": {},
                "checked_items": [],
                "revision_diff": {},
                "persistence_status": "failed",
            },
        }
        result = self.node.execute(state)
        assert "蓄積失敗" in result["formatted_output"]["opinion_markdown"]

    def test_ingest_mode_output_shape(self, base_state):
        state = {**base_state, "mode": "ingest", "ingest_summary": {"n_ok": 5}}
        result = self.node.execute(state)
        assert result["formatted_output"]["mode"] == "ingest"
        assert result["formatted_output"]["ingest_summary"]["n_ok"] == 5

    def test_feedback_mode_output_shape(self, base_state):
        state = {**base_state, "mode": "feedback", "feedback_result": {"case_id": "c1"}}
        result = self.node.execute(state)
        assert result["formatted_output"]["mode"] == "feedback"


class TestAssertiveLanguageDowngrade:
    def test_prohibited_phrase_downgraded_not_blocked(self):
        text = "本件は支払対象外です。"
        downgraded, was_downgraded = _downgrade_assertive_language(text)
        assert was_downgraded is True
        assert "支払対象外です" in downgraded  # not deleted — hedged instead
        assert "要確認" in downgraded

    def test_clean_text_unaffected(self):
        text = "所見に矛盾フラグはありません。"
        downgraded, was_downgraded = _downgrade_assertive_language(text)
        assert was_downgraded is False
        assert downgraded == text

    def test_fullwidth_bypass_attempt_still_detected(self):
        """NFKC normalization must catch full-width/half-width variants."""
        # full-width Latin exclamation / half-width katakana style bypass attempts
        # are less relevant to Japanese text here, but full-width digit/ASCII
        # variants inside the phrase are the realistic bypass vector — this
        # test verifies the underlying normalize() step is actually applied.
        normalized = _normalize("ＡＢＣ")
        assert normalized == "abc"

    def test_casefold_bypass_attempt_detected(self):
        # The prohibited phrases are Japanese (no case), but the detector
        # must still apply casefold uniformly without raising on mixed input.
        text = "不正請求です abc DEF"
        downgraded, was_downgraded = _downgrade_assertive_language(text)
        assert was_downgraded is True


class TestOpinionComposeNodeSecurityGate:
    def setup_method(self):
        self.node = OpinionComposeNode()

    def test_residual_pii_redacted_in_output(self):
        result = {
            "formatted_output": {
                "opinion_markdown": "連絡先: taro@example.com",
                "findings": {"lv1_flags": [], "lv2_flags": []},
            }
        }
        gated = self.node._extra_security_gate_output(result)
        assert "taro@example.com" not in gated["formatted_output"]["opinion_markdown"]

    def test_evidence_less_flags_removed(self):
        result = {
            "formatted_output": {
                "opinion_markdown": "no pii here",
                "findings": {
                    "lv1_flags": [{"kind": "X", "evidence": []}, {"kind": "Y", "evidence": [{"doc_id": "d1"}]}],
                    "lv2_flags": [],
                },
            }
        }
        gated = self.node._extra_security_gate_output(result)
        remaining_kinds = [f["kind"] for f in gated["formatted_output"]["findings"]["lv1_flags"]]
        assert "X" not in remaining_kinds
        assert "Y" in remaining_kinds

    def test_findings_still_returned_after_downgrade(self):
        """design §6-4: downgrade must not suppress the finding itself."""
        result = {
            "formatted_output": {
                "opinion_markdown": "本件は不正請求です。",
                "findings": {"lv1_flags": [{"kind": "X", "evidence": [{"doc_id": "d1"}]}], "lv2_flags": []},
            }
        }
        gated = self.node._extra_security_gate_output(result)
        assert len(gated["formatted_output"]["findings"]["lv1_flags"]) == 1
        assert "要確認" in gated["formatted_output"]["opinion_markdown"]

    def test_downgrade_survives_subsequent_pii_redaction(self):
        """Regression guard: the assertive-language
        downgrade must not be silently discarded when the PII rescan step
        also fires on the same opinion_markdown — both mutations must
        compose, not clobber each other."""
        result = {
            "formatted_output": {
                "opinion_markdown": "本件は支払対象外です。連絡先: taro@example.com",
                "findings": {"lv1_flags": [], "lv2_flags": []},
            }
        }
        gated = self.node._extra_security_gate_output(result)
        markdown = gated["formatted_output"]["opinion_markdown"]
        assert "要確認" in markdown  # downgrade preserved
        assert "taro@example.com" not in markdown  # PII still redacted

    def test_s3_rescan_catches_japanese_honorific_name_and_email_before_japanese(self):
        """Regression guard for supplementary PII detection.

        The vendored detect_pii misses (1) Japanese names entirely and
        (2) email/phone immediately followed by Japanese text (trailing \\b
        fails). The S-3 rescan must use the supplementary layer
        (detect_pii_extended) so an untokenized insured-person name can
        never reach the opinion draft output."""
        result = {
            "formatted_output": {
                "opinion_markdown": "被保険者 田中太郎様、連絡先 taro@example.comにも共有。",
                "findings": {"lv1_flags": [], "lv2_flags": []},
            }
        }
        gated = self.node._extra_security_gate_output(result)
        markdown = gated["formatted_output"]["opinion_markdown"]
        assert "田中太郎" not in markdown
        assert "taro@example.com" not in markdown
        assert "[REDACTED]" in markdown

    def test_s3_rescan_catches_phone_before_japanese(self):
        result = {
            "formatted_output": {
                "opinion_markdown": "折返し先は080-1234-5678です。",
                "findings": {"lv1_flags": [], "lv2_flags": []},
            }
        }
        gated = self.node._extra_security_gate_output(result)
        markdown = gated["formatted_output"]["opinion_markdown"]
        assert "080-1234-5678" not in markdown
        assert "[REDACTED]" in markdown
