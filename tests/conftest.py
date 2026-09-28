"""INS-C2-906 — shared test fakes (no real LLM / DB / secrets calls anywhere)."""

from __future__ import annotations

import json
from typing import Any

import pytest


class FakeLLM:
    """Test double for shared.services.llm.base_llm.BaseLLM.

    Returns a fixed structured JSON response by default; tests override
    `content` or `content_fn` to drive different flag-detection scenarios.
    """

    def __init__(self, content: str = '{"flags": []}', content_fn=None):
        self._content = content
        self._content_fn = content_fn
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list) -> dict:
        self.calls.append(messages)
        content = self._content_fn(messages) if self._content_fn else self._content
        return {"content": content, "tool_calls": [], "model": "fake-llm", "usage": {}}

    def stream(self, messages: list):
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list) -> "FakeLLM":
        return self


class FailingLLM:
    """Test double that always raises — exercises design §8 LLM-failure fallback."""

    def complete(self, messages: list) -> dict:
        raise RuntimeError("simulated LLM transport failure")

    def stream(self, messages: list):
        raise RuntimeError("simulated LLM transport failure")

    def bind_tools(self, tools: list) -> "FailingLLM":
        return self


class InMemoryCaseService:
    """Test double for src.services.assessment_case_service.AssessmentCaseService.

    Keyed by (case_id, revision); enforces the unique-constraint semantics
    persist_case's collision-retry logic depends on.
    """

    def __init__(self):
        self.rows: dict[tuple[str, int], dict[str, Any]] = {}
        self.row_hashes: set[str] = set()
        self.fail_reads = False  # simulate DB-unreachable on read path

    def get_latest_revision(self, case_id: str):
        if self.fail_reads:
            raise ConnectionError("simulated DB outage")
        revs = [r for (cid, _rev), r in self.rows.items() if cid == case_id]
        if not revs:
            return None
        return max(revs, key=lambda r: r["revision"])

    def get_revision(self, case_id: str, revision: int):
        if self.fail_reads:
            raise ConnectionError("simulated DB outage")
        return self.rows.get((case_id, revision))

    def insert_case_revision(self, record: dict[str, Any]) -> None:
        key = (record["case_id"], record["revision"])
        if key in self.rows:
            raise RuntimeError(f"unique constraint violation on {key}")
        self.rows[key] = dict(record)

    def update_case_verdict(self, case_id: str, revision: int, updates: dict[str, Any]) -> dict[str, Any]:
        key = (case_id, revision)
        if key not in self.rows:
            raise KeyError(f"no such revision {key}")
        self.rows[key].update(updates)
        return updates

    def delete_case_revision(self, case_id: str, revision: int) -> None:
        self.rows.pop((case_id, revision), None)

    def query_population(self, policy_type: str, coverage_item: str, exclude_case_id: str | None = None):
        return [
            r
            for (cid, _rev), r in self.rows.items()
            if r.get("policy_type") == policy_type
            and r.get("coverage_item") == coverage_item
            and r.get("final_verdict") in ("paid", "partial", "denied")
            and cid != exclude_case_id
        ]

    def query_claimant_records(self, claimant_key: str, lookback_days: int):
        return [r for r in self.rows.values() if r.get("claimant_key") == claimant_key]

    def insert_ingest_row(self, record: dict[str, Any]) -> bool:
        h = record.get("source_row_hash", "")
        if h and h in self.row_hashes:
            return False
        if h:
            self.row_hashes.add(h)
        key = (record.get("case_id") or h, record.get("revision", 1))
        self.rows[key] = dict(record)
        return True

    def row_hash_exists(self, source_row_hash: str) -> bool:
        return source_row_hash in self.row_hashes

    # test helper (not part of the AssessmentCaseService protocol)
    def seed(self, record: dict[str, Any]) -> None:
        self.rows[(record["case_id"], record["revision"])] = dict(record)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def failing_llm() -> FailingLLM:
    return FailingLLM()


@pytest.fixture
def case_service() -> InMemoryCaseService:
    return InMemoryCaseService()


@pytest.fixture
def base_state() -> dict:
    """Minimal AgentState-shaped dict covering the SubgraphContext fields
    InvocationContext.from_state() requires, plus VERIFIED_EXTERNAL trust —
    the standard trust level for this template's nodes (design §9)."""
    return {
        "correlation_id": "test-corr",
        "session_id": "test-session",
        "thread_id": "test-thread",
        "trace_id": "test-trace",
        "caller_trust_level": "VERIFIED_EXTERNAL",
        "caller_id": "",
        "hitl_allowed": True,
        "node_history": [],
        "error_log": [],
    }


def assess_payload(case_id: str = "", is_recheck: bool = False, **overrides) -> str:
    """Build the JSON payload the outer GraphNode.extract_input() would hand
    to the inner graph's ParsePayloadNode for assess mode."""
    payload = {
        "mode": "assess",
        "case_id": case_id,
        "is_recheck": is_recheck,
        "documents": overrides.pop(
            "documents",
            [
                {
                    "doc_id": "d1",
                    "doc_type": "invoice",
                    "text": "invoice text",
                    "fields": {"claimed_amount": 1000.0, "claim_target": "vehicle"},
                }
            ],
        ),
        "contract": overrides.pop(
            "contract",
            {
                "coverage_limit": 5000,
                "deductible": 0,
                "policy_start": "2026-01-01",
                "policy_end": "2026-12-31",
                "policy_type": "auto",
                "coverage_items": ["collision"],
            },
        ),
        "claimant_key": overrides.pop("claimant_key", "claimant-key-1"),
    }
    payload.update(overrides)
    return json.dumps(payload, default=str)


@pytest.fixture
def agent_config(case_service: InMemoryCaseService) -> dict:
    """実 LLM 1回転チェック用 config。

    `tests/integration/test_outer_graph.py::_make_agent()` と同じ seed なしの
    InMemoryCaseService と limits/lv3 設定を使う。ハーネスが `config["llm"]` を
    live client に差し替える。
    """
    return {
        "max_retry": 1,
        "case_service": case_service,
        "lv3": {"min_population_n": 30, "similar_cases_top_n": 5, "repeat_lookback_days": 365},
        "limits": {"max_documents": 12, "max_total_chars": 400_000},
    }


@pytest.fixture
def case_payload() -> str:
    """outer graph integration tests と同じく本文は空、業務入力は input_context で渡す。"""
    return ""


@pytest.fixture
def live_input_context() -> dict:
    """`tests/integration/test_outer_graph.py::test_new_case_assess_success` と同形。

    ただし本文は実物の証憑テキストにしてある。統合テストは `text: "x"` の
    最小ダミーで invoke 経路の形だけを見ているが、その入力で実 LLM を回すと
    StructureDocsNode が抽出する field が空になり、Lv1 は全件
    "insufficient data"、Lv2 は照合対象なしで `{"flags": []}` を返す。
    それでも harness の非空判定は Lv1 の「必須項目なし」所見で満たされるため、
    **業務の中核が一度も走らないまま PASS** する（2026-08-20 に実測）。

    そこで assess の中核が実際に走る 3 点セットを与える:
      * Lv1 決定論チェックが計算可能（金額が補償上限超過・事故日が保険期間内）
      * Lv2 交差照合に矛盾が実在（初診日 03-02 が事故日 03-14 より前）
      * 必須項目が doc_type ごとに揃っている（`REQUIRED_FIELDS_BY_DOC_TYPE`）
    値は LLM が本文から抽出する前提で、`fields` を手で与えることはしない。
    """
    return {
        "mode": "assess",
        "case_id": "",
        "documents": [
            {
                "doc_id": "d1",
                "doc_type": "accident_report",
                "text": (
                    "事故報告書\n"
                    "事故発生日: 2026年3月14日 午後6時20分ごろ\n"
                    "発生場所: 市道交差点手前\n"
                    "事故状況: 信号待ちで停止中の当方車両に後続車が追突し、"
                    "リアバンパーおよびトランクリッドが変形した。当方は被害者側であり、"
                    "相手方が過失を認めている。\n"
                    "対象: 車両（リアバンパー）"
                ),
            },
            {
                "doc_id": "d2",
                "doc_type": "medical_certificate",
                "text": (
                    "診断書\n"
                    "傷病名: 頸椎捻挫\n"
                    "初診日: 2026年3月2日\n"
                    "経過: 受傷後より頸部痛および可動域制限を認める。"
                    "以後 4 週間の通院加療を要する見込み。"
                ),
            },
            {
                "doc_id": "d3",
                "doc_type": "invoice",
                "text": (
                    "請求書\n"
                    "請求対象: 車両（リアバンパー）修理費\n"
                    "請求金額合計: 780,000円\n"
                    "内訳: 部品代 520,000円 / 塗装費 160,000円 / 工賃 100,000円"
                ),
            },
        ],
        "contract": {
            "coverage_limit": 500000,
            "deductible": 20000,
            "policy_start": "2026-01-01",
            "policy_end": "2026-12-31",
            "policy_type": "auto",
            "coverage_items": ["collision"],
        },
        "claimant_key": "claimant-1",
    }
