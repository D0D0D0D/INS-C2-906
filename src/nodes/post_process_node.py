"""AgentCore Platform v1.0 — INS-C2-906 OpinionComposeNode (post_process)"""

# docs/02_design.md §6-4 / §9 / §10 — builds both a human Markdown opinion
# draft and a machine-readable structured JSON (always both, design point
# 6). S-3 hook (_extra_security_gate_output):
#   (a) assertive-language downgrade — NFKC-normalize + casefold BEFORE
#       matching prohibited phrases (bypass-proofing full/half-width and
#       case variants), downgrade matched sentences to hedged "要確認"
#       phrasing. This is a DOWNGRADE, not a block — findings must still be
#       returned (design §6-4).
#   (b) residual-PII rescan on the draft output (final defense against
#       token-leak).
#   (c) evidence-less-flag removal verification (defense in depth — Lv2
#       should already have dropped these; re-verify here).

from __future__ import annotations

import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.pii_vault import detect_pii_extended

# Prohibited assertive/definitive phrases (design §6-4). Matched after
# NFKC normalization + casefold so full-width/half-width and case variants
# cannot bypass the check.
_PROHIBITED_PHRASES = (
    "支払対象外です",
    "不正請求です",
    "支払いは認められません",
    "詐欺です",
    "虚偽の申告です",
)

_HEDGE_SUFFIX = "（要確認 — 断定不可、査定者による確認が必要）"


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _downgrade_assertive_language(text: str) -> tuple[str, bool]:
    """Scan `text` for prohibited assertive phrases and hedge them.

    Returns (possibly-modified text, whether any downgrade occurred).
    Matching is done on NFKC+casefold normalized text to prevent bypass via
    full-width/half-width or case variation, but the replacement is applied
    to a normalized copy of the original text so downstream length/offset
    assumptions stay simple (this template does not need byte-exact
    preservation of the original script form for this hedge annotation).
    """
    normalized = _normalize(text)
    downgraded = False
    result = text
    for phrase in _PROHIBITED_PHRASES:
        normalized_phrase = _normalize(phrase)
        if normalized_phrase in normalized:
            downgraded = True
            # Replace on the original text using a normalized-aware search:
            # since NFKC normalization rarely changes string length for
            # Japanese text in practice here, we fall back to a direct
            # substring check against the raw text first; if not found
            # verbatim (i.e. only found after normalization), append a
            # hedge annotation rather than attempting an in-place rewrite.
            if phrase in result:
                result = result.replace(phrase, phrase + _HEDGE_SUFFIX)
            else:
                result = f"{result} {_HEDGE_SUFFIX}"
    return result, downgraded


class OpinionComposeNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict:
        mode = state.get("mode", "assess")
        result = state.get("result") or {}

        if mode == "assess":
            output = self._compose_assess_output(state, result)
        elif mode == "ingest":
            output = {"mode": "ingest", "ingest_summary": state.get("ingest_summary", {})}
        else:
            output = {"mode": "feedback", "feedback_result": state.get("feedback_result", {})}

        emit_trace_event("opinion_draft_composed", {"mode": mode}, state)

        # AgentBaseGraph.get_output() reads formatted_output (falling back to
        # result) as the outer `output` key of agent.invoke()'s response —
        # this node must write formatted_output, not just a local `output`
        # variable, or the final response silently falls back to the raw
        # compose_findings dict instead of this node's Markdown+JSON draft.
        return {"formatted_output": output, "status": AgentStatus.SUCCESS.value}

    def _compose_assess_output(self, state: AgentState, result: dict) -> dict:
        lv1_flags = result.get("lv1_flags", state.get("lv1_flags", []))
        lv2_flags = result.get("lv2_flags", state.get("lv2_flags", []))
        lv3_result = result.get("lv3_result", state.get("lv3_result", {}))
        checked_items = result.get("checked_items", state.get("checked_items", []))
        revision_diff = result.get("revision_diff", state.get("revision_diff", {}))
        persistence_status = result.get("persistence_status", state.get("persistence_status", "skipped"))
        case_id = result.get("case_id", state.get("case_id", ""))
        revision = result.get("revision", state.get("revision", 1))

        all_flags = list(lv1_flags) + list(lv2_flags)
        markdown = self._build_markdown(
            case_id, revision, all_flags, checked_items, lv3_result, revision_diff, persistence_status
        )

        return {
            "mode": "assess",
            "case_id": case_id,
            "revision": revision,
            "opinion_markdown": markdown,
            "findings": {
                "lv1_flags": lv1_flags,
                "lv2_flags": lv2_flags,
                "lv3_result": lv3_result,
                "checked_items": checked_items,
                "revision_diff": revision_diff,
            },
            "persistence_status": persistence_status,
        }

    @staticmethod
    def _build_markdown(
        case_id: str,
        revision: int,
        flags: list[dict[str, Any]],
        checked_items: list[dict[str, Any]],
        lv3_result: dict[str, Any],
        revision_diff: dict[str, Any],
        persistence_status: str,
    ) -> str:
        lines = [f"# 査定所見ドラフト — case_id: {case_id} (revision {revision})", ""]

        # usability point 1 (design §10): always show what was checked, pass or fail.
        lines.append("## 検査カバレッジ")
        for item in checked_items:
            mark = "○" if item.get("passed") else ("△" if item.get("passed") is None else "✗")
            lines.append(f"- [{mark}] {item.get('check')}")
        lines.append("")

        lines.append("## 論点 → 根拠 → 推奨アクション")
        if not flags:
            lines.append("- 検出された矛盾フラグはありません。")
        for flag in flags:
            lines.append(f"### {flag.get('kind')}")
            lines.append(f"- 理由: {flag.get('reason', '')}")
            for ev in flag.get("evidence", []):
                lines.append(f"  - 根拠 ({ev.get('doc_id')}): 「{ev.get('quote')}」（{ev.get('field')}）")
            lines.append("- 推奨アクション: 追加照会文案を作成し、該当書類の再確認を依頼してください。")
        lines.append("")

        lines.append("## Lv3 異常度")
        if lv3_result.get("degraded"):
            lines.append(f"- 参照不可: {lv3_result.get('degraded')}")
        elif lv3_result.get("anomaly_score") is not None:
            lines.append(
                f"- 母集団 n={lv3_result.get('population_n')}、中央値={lv3_result.get('median')}、"
                f"本件パーセンタイル={lv3_result.get('percentile')}"
            )
        lines.append("")

        if revision_diff:
            lines.append("## 前回リビジョンとの差分")
            counts = revision_diff.get("counts", {})
            lines.append(
                f"- 解消: {counts.get('resolved', 0)} / 新規: {counts.get('new', 0)} / "
                f"継続: {counts.get('continuing', 0)}"
            )
            if revision_diff.get("dismissed_recurrence"):
                lines.append(f"- 前回査定者が dismiss 済みのフラグの再検出（再考の材料）: {revision_diff['dismissed_recurrence']}")
            lines.append("")

        if persistence_status == "failed":
            lines.append(
                "## 注意: 蓄積失敗\n"
                "このリビジョンは蓄積されていないため `/feedback` は受理されません。"
                "記録するには同一書類で再実行してください（新リビジョンとして記録されます）。"
            )
        elif persistence_status == "read_only":
            lines.append(
                "## 注意: 読み取り専用環境\n"
                "この環境では査定履歴を蓄積しません。`/feedback` は利用できず、"
                "この実行によって異常度判定の母集団も増加しません。"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # S-3 extension hook (design §6-4)
    # ------------------------------------------------------------------

    def _extra_security_gate_output(self, result: dict) -> dict:
        output = result.get("formatted_output")
        if not isinstance(output, dict):
            return result

        # (a) assertive-language downgrade. Must run BEFORE the PII rescan and
        # each step must read output["opinion_markdown"] freshly (not a stale
        # local snapshot) — otherwise the PII-redaction step below would
        # overwrite this step's hedged text with a version derived from the
        # pre-downgrade string, silently discarding the downgrade.
        markdown = output.get("opinion_markdown")
        if isinstance(markdown, str):
            downgraded_markdown, was_downgraded = _downgrade_assertive_language(markdown)
            if was_downgraded:
                output["opinion_markdown"] = downgraded_markdown
                emit_trace_event("s3_assertive_language_downgraded", {"case_id": output.get("case_id")}, result)

        # (b) residual-PII rescan on the draft — last defense against token
        # leak. Re-read from `output` (post-downgrade) rather than reusing
        # the pre-downgrade `markdown` local. Uses detect_pii_extended (not
        # bare detect_pii): the vendored detector misses honorific Japanese
        # names and email/phone immediately followed by Japanese text;
        # without the supplementary pass, an
        # untokenized insured-person name would reach the opinion draft.
        markdown = output.get("opinion_markdown")
        if isinstance(markdown, str):
            residual = detect_pii_extended(markdown)
            if residual:
                # Redact in place rather than blocking — findings must still return.
                masked = markdown
                for f in reversed(residual):
                    masked = masked[: f["start"]] + "[REDACTED]" + masked[f["end"] :]
                output["opinion_markdown"] = masked
                emit_trace_event(
                    "s3_residual_pii_redacted", {"finding_count": len(residual)}, result
                )

        # (c) evidence-less-flag removal verification (defense in depth).
        # findings.lv1_flags/lv2_flags exist only in
        # assess-mode output (_compose_assess_output). For ingest/feedback the
        # `{}` fallback below makes this step a deliberate no-op — if a future
        # mode ever carries flag lists under a different key, extend this step
        # explicitly rather than relying on that fallback.
        findings = output.get("findings", {})
        for flag_field in ("lv1_flags", "lv2_flags"):
            flags = findings.get(flag_field)
            if isinstance(flags, list):
                findings[flag_field] = [f for f in flags if f.get("evidence")]

        result["formatted_output"] = output
        return result
