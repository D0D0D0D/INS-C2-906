# Test Specification — INS-C2-906 ClaimConsistencyCheckAgent

## Test Strategy

- Coverage target: all framework-compliance and proof-of-boundary items + all domain-specific
  test items called out in `docs/02_design.md` §13-1.
- Test types: Unit (per-node, per-service-function) / Integration (full outer graph,
  full inner graph) / Proof-of-Boundary (framework contract verification).
- All LLM/DB/secrets access is behind test doubles (`tests/conftest.py`:
  `FakeLLM`, `FailingLLM`, `InMemoryCaseService`) — zero real external calls in
  any test.
- Result column below reflects the last full local run: **196 passed, 0 failed,
  2 skipped** (`pytest -q tests/`).

## Framework Compliance Tests

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict (`src/schemas/state.py`, `src/schemas/claim_workflow_state.py`) | Type check pass, no Pydantic/dataclass | PASS — `tests/unit/test_framework_compliance.py::TestTC01StateContract` |
| TC-02 | `SecurityViolationError` fires on invalid input (S-5 class-level JWT scan) | Error raised at class definition time for a JWT-shaped class attribute | PASS — `TestTC02SecurityViolationError` |
| TC-03 | No JWT/Credential in `src/` | 0 credential-pattern matches (`gate-credential-scan` CI + local AST/regex test) | PASS — `TestTC03NoCredentialsInSrc` |
| TC-04 | `InvocationContext` via `configurable`/`from_state()` only | No manual `InvocationContext(...)` construction inside node `execute()` bodies | PASS — `TestTC04InvocationContextConfigurableOnly` |
| TC-05 | `emit_trace_event()` called inside `execute()` (or the node's sole always-invoked hook) for every node module | ≥1 domain event per node module, regardless of side effect | PASS — `TestTC05AuditLogging` (covers all `src/nodes/**/*.py`, including `ClaimWorkflowGraphNode.merge_output()`) |
| TC-06 | `_security_gate_input()` is `@final` on `FunctionNode` — non-bypassable | `TypeError` at class definition if overridden; our `FunctionNode` subclasses never override it | PASS — `TestTC06SecurityGateInputNonBypassable` |
| TC-07 | `_security_gate_output()` is `@final` on `FunctionNode` — non-bypassable | `TypeError` at class definition if overridden; our `FunctionNode` subclasses never override it | PASS — `TestTC07SecurityGateOutputNonBypassable` |
| TC-08 | `required_trust_level` declared explicitly and enforced | `ClaimIntakeNode` / `ClaimWorkflowGraphNode` / `OpinionComposeNode` (and all inner nodes) declare `VERIFIED_EXTERNAL`; `ANONYMOUS` caller refused at `__call__()`; PB-6 `test_s1_denial_refuses_execution_before_execute` proves `execute()` was not called | PASS — `TestTC08RequiredTrustLevelEnforced` + `tests/proof_of_boundary/test_pb_invoke_order.py` |

## Proof-of-Boundary Tests

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | `BaseNode` → `AuditLogger` | `emit_trace_event()` fires from `shared.utils.audit_logger` | No silent failures | PASS — exercised by TC-05 + PB-6 |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass/arbitrary objects | PASS — `tests/proof_of_boundary/test_state_safety.py` |
| PB-3 | L1 → External service (`assessment_cases`) | Real data retrieval through `AssessmentCaseService` (in-memory implementation of the same Protocol as `SqlAssessmentCaseService` — no live tenant DB in CI) | Real (non-inline-mocked) data flows node → service → retrieval | PASS — `tests/proof_of_boundary/test_pb3_external_service.py` |
| PB-4 | Import isolation | Template does not import Level 0 | AST scan: 0 violations | PASS — `tests/proof_of_boundary/test_import_isolation.py` |
| PB-5 | Checkpoint safety | Saved checkpoint contains no JWT/Pydantic objects | N/A — `memory_enabled: false`, no checkpointer configured (design §4-3); state-safety scan (PB-2) is the applicable structural equivalent | PASS (via PB-2) |
| PB-6 | Invoke execution order | S-1 → S-4 `node_start` → S-2 → `execute()` → S-3 → S-4 `node_complete`, for every discovered node class under `src/nodes/` (including `src/nodes/inner/` and the `ClaimWorkflowGraphNode` GraphNode) | Order verification pass | PASS — `tests/proof_of_boundary/test_pb_invoke_order.py` |
| PB-7 | HITL interrupt propagation | N/A — `hitl.enabled: false` (design §4-3 Design Decision Record) | Skipped by design, not applicable | N/A |

## Domain-Specific Test Items (design §13-1)

| Item | Test | Expected Result | Result |
|------|------|----------------|--------|
| Revision uniqueness / concurrent collision | `(case_id, revision)` unique-constraint collision → reload latest → reassign revision → **recompute** diff against the new prior → retry INSERT exactly once; second failure → `persistence_status="failed"`, not raised | PASS — `tests/unit/test_inner_persist_case_node.py::TestPersistCaseNodeCollisionRetry` (3 tests: successful retry, retry-also-fails, findings never blocked) |
| Revision assignment (1 / n+1) | New case → `revision=1`, no DB read; recheck → `revision = max(revision)+1` | PASS — `tests/unit/test_inner_load_case_thread_node.py`, `tests/unit/test_inner_persist_case_node.py::TestPersistCaseNodeBasics` |
| `flag_key` stability | Same logical contradiction re-detected after document resend with different quote wording / anchor order from the LLM still yields the same `flag_key`; enum-external `kind` discarded | PASS — `tests/unit/test_inner_lv2_cross_doc_node.py` (`test_flag_key_stable_across_reruns_with_different_quote_wording`, `test_flag_key_stable_regardless_of_anchor_order_from_llm`, `test_out_of_enum_kind_discarded`), `tests/unit/test_taxonomy.py` |
| `compute_revision_diff` purity | Same input → same output; no LLM/DB calls (AST-checked); 3-way classification (resolved/new/continuing); dismissed-flag recurrence annotated, never auto-suppressed; `revision=1` → empty diff | PASS — `tests/unit/test_revision_diff.py` (7 tests) |
| Lv3 population/similar-case exclusion | Current `case_id`'s full revision history excluded from population AND similar cases; repeat-claim count is distinct `case_id` (not revision-inflated) | PASS — `tests/unit/test_inner_lv3_anomaly_node.py::test_excludes_current_case_id_revisions_from_population`, `test_repeat_claim_counts_distinct_case_id`; `tests/unit/test_lv3_stats.py::TestRepeatClaimPattern` |
| Document-resend contract / fail-fast | No document-body/free-text-opinion column anywhere in the persistence schema (structural AST/source scan); recheck-path code never reads document text from the DB; `unknown_case_id` / `unknown_revision` errors; DB-unreachable-on-recheck → `case_thread_unavailable`, `status=error`, no execution, no new-case fallback | PASS — `tests/proof_of_boundary/test_document_non_persistence.py` (structural); `tests/unit/test_inner_load_case_thread_node.py::test_recheck_unknown_case_id_errors`, `test_db_unreachable_on_recheck_fails_fast_not_new_case`, `test_recheck_without_case_service_fails_fast_not_new_case`; `tests/unit/test_inner_feedback_nodes.py::TestValidateCaseNode::test_unknown_revision_errors` |
| Persistence backend contract | Revision round-trip/latest selection, feedback update/delete, confirmed-only population filtering, claimant lookback, and ingest deduplication through the public repository port | PASS — `tests/contract/test_assessment_case_repository_contract.py` |
| Feedback revision targeting | Default = latest revision; explicit `revision` targets a specific row; idempotent (resend same payload → same end state, no duplicate side effects); different verdict resend → last-write-wins | PASS — `tests/unit/test_inner_feedback_nodes.py` (9 tests) |
| Lv1 checked-item completeness | Every Lv1 check records a `checked_items` entry, pass/fail/insufficient-data (`passed: null`), never silently omitted | PASS — `tests/unit/test_lv1_checks.py` |
| PII tokenization boundary | Server-layer tokenization/resolve round-trips correctly; per-request isolation (one vault's tokens do not resolve against another); medical content left untouched; `claimant_key` one-way derivation | PASS — `tests/unit/test_pii_vault.py` (10 tests) |
| PII supplementary detection layer | Honorific-suffixed Japanese names (様/氏/殿/さん等) and email/phone/policy-number immediately followed by Japanese text are tokenized (vendored `detect_pii` misses both — trailing `\b` fails before Japanese word chars); overlap dedup prevents double-tokenization; medical narrative not over-redacted; known limit (bare name without honorific → not detected) pinned; S-3 rescan uses the same extended layer so these patterns cannot reach the opinion draft | PASS — `tests/unit/test_pii_vault.py::TestSupplementaryDetection` (8 tests), `tests/unit/test_post_process_node.py::TestOpinionComposeNodeSecurityGate::test_s3_rescan_catches_japanese_honorific_name_and_email_before_japanese`, `test_s3_rescan_catches_phone_before_japanese` |
| S-3 assertive-language downgrade | Prohibited phrase hedged (not deleted/blocked) — finding still returned; NFKC+casefold normalization applied before matching (full-width/case bypass resistance) | PASS — `tests/unit/test_post_process_node.py::TestAssertiveLanguageDowngrade` (4 tests), `TestOpinionComposeNodeSecurityGate::test_findings_still_returned_after_downgrade` |
| mode_router state annotation (critical regression guard) | Inner graph's `mode_router` / `_continue_or_end` MUST be annotated with `ClaimWorkflowState`, not `AgentState` — verified both structurally (`typing.get_type_hints()`) and behaviorally (each of the 3 modes reaches its expected node sequence through the real compiled `StateGraph`) | PASS — `tests/integration/test_domain_workflow_graph.py::TestModeRouterAnnotation`, `TestModeRoutingBehavior` |
| GraphNode subgraph caching | `get_subgraph()` returns the same cached instance across calls (never rebuilt per `execute()`) | PASS — `tests/unit/test_main_node.py::test_get_subgraph_is_cached_not_rebuilt` |
| Ingest row-unit commit / dedup | Row-unit success/failure counted independently; duplicate `source_row_hash` on reingest skipped, not re-inserted; `dry_run` performs no writes | PASS — `tests/unit/test_inner_ingest_nodes.py::TestStoreRecordsNode` |

## Test Execution Summary

- Execution date: 2026-08-20 (full local suite)
- Total tests: 214
- Pass: 212 / Fail: 0 / Skip: 2 (PB-7 is not applicable because HITL is disabled)
- Local commands run:
  - `pytest -q tests/` → 212 passed, 2 skipped
- Coverage: not numerically measured in this pass (no `pytest-cov` threshold
  configured); every node module and every pure service function has at least
  one success-path and one error/edge-path test under the documented coverage
  guidance.

## 実 LLM 1 回転検証（live LLM check）

`config/agent.yaml` の `generation_mode: "llm"` 宣言に対する実挙動の裏取り。

| 項目 | 値 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | Azure OpenAI のモデルを `BaseLLM` 実装として注入 |
| SDK | `agenticstar-agentcore[anthropic]==1.0.1`（実施当時の `AGENTCORE_WHEEL_SPEC`。CI は後に 1.0.3 へ更新） |
| 注入シーム | `config["llm"]` → `ClaimCheckWorkflowGraph.register_nodes()` → `StructureDocsNode` / `Lv2CrossDocNode`（`call_structured()` 経由で `llm.complete(messages)`） |
| LLM 呼び出し回数 | 4（文書 3 通の構造化 × 3 + Lv2 交差照合 × 1） |
| 結果 | ✅ `status: success` |
| 非空だった出力キー | `mode`, `case_id`, `revision`, `opinion_markdown`, `findings`, `persistence_status` |

### 🔴 最初の入力では「中核が走らないまま PASS」していた

`tests/conftest.py` の `live_input_context` は当初、統合テストと同じ `text: "x"` の最小ダミーだった。
その入力で実 LLM を回すと:

- `StructureDocsNode` が抽出する field が空（実応答 `{}`）
- Lv1 は `amount_within_coverage_limit` / `incident_within_policy_period` とも `insufficient data`
- `Lv2CrossDocNode` は照合対象が 1 通しかなく実応答 `{"flags": []}`

それでも harness の非空判定は Lv1 の「必須項目なし」所見で満たされ **✅ になる**。
LLM は 2 回呼ばれているのに、業務の中核（文書間矛盾の検出）は一度も動いていない。

そこで fixture を業務実体のある入力に差し替えた（事故報告書・診断書・請求書の 3 通、
初診日が事故日より 12 日前という矛盾を含み、請求額 78 万が補償上限 50 万を超える）。実 LLM の結果:

| 段階 | 実 LLM が出したもの |
|---|---|
| 構造化（3 通） | `incident_date: 2026-03-14` / `first_visit_date: 2026-03-02` / `injury_name: 頸椎捻挫` / `claimed_amount: 780000` / `claim_target: 車両（リアバンパー）` / `party_role: 被害者側` |
| Lv1（決定論） | `amount_within_coverage_limit: false`（780,000 > 500,000）、`incident_within_policy_period: true`、`deductible_math: true` |
| Lv2（交差照合） | `LV2_DATE_CONFLICT`（severity high・初診日が事故日より前）、`LV2_SUBJECT_MISMATCH`（車両損害 vs 身体傷害）— いずれも両文書からの逐語 evidence つき |

> 実 LLM のため実行ごとに揺れる。2 回実行し、**仕込んだ日付矛盾（`LV2_DATE_CONFLICT`）は 2 回とも検出**、
> `LV2_SUBJECT_MISMATCH` は 1 回目のみだった。Lv1 の決定論判定は 2 回とも同一。

### この検証で見つかった欠陥（修正済み）

**`call_structured()` が JSON object 以外を通していた。** `json.loads()` の結果を型検査せず返していたため、
実 LLM が `[]`（配列）や scalar を返すと呼び出し側へ非 mapping が渡る。`Lv2CrossDocNode` は
`raw.get(...)` を実行するので `AttributeError` になり、**設計 §8 が定める `LLMInvocationError` の
縮退経路の外**で落ちていた。JSON として妥当なので既存の `json.JSONDecodeError` ハンドラでは捕まらない。
修正は `call_structured()` で dict であることを検査し、既存の `LLMInvocationError` を投げるもの
（型名のみ報告・応答本文は出さない）。3 つの呼び出し側（structure / Lv2 / map_columns）は
いずれも既にこの例外を処理している。

### 追加テスト

| テスト | 内容 |
|---|---|
| `tests/unit/test_llm_client.py::TestCallStructuredResponseShape` | 配列・scalar・非 JSON・空応答が `LLMInvocationError` になる。拒否メッセージは型名のみで本文を出さない |
| `tests/unit/test_llm_client.py::TestNonObjectResponseDegradesInsteadOfCrashing` | 配列応答が full graph で設計 §8 の Lv1 のみ縮退に落ち、run は success のまま |
| `tests/integration/test_outer_graph.py::TestOuterGraphLlmExtractionReachesDeterministicChecks` | 「LLM が抽出した値 → Lv1 の判定」の配線を実グラフで検証（従来の統合テストは `text: "x"` のため未検証だった） |

追加した統合テストの double は **実 LLM が返すもの（抽出結果）だけ**を返し、判定は Lv1 が計算する。
`fields` をテストが文書に植えることはしない。呼び分けは system prompt で行い、認識できない prompt が
来たら検知する（user 本文で分岐すると Lv2 の prompt が抽出済み本文を含むため誤配送し、
テストが緑のまま無意味になる）。

### 再現手順

`mrb_live_llm_check.py` は **repo の外に置く検証ハーネス**で、テンプレートには同梱しない
（実 LLM への接続情報を要し、CI にも入れない）。repo 側が用意するのは `tests/conftest.py` の
`agent_config` / `case_payload` / `live_input_context` fixture で、ハーネスはそれを読んで
`config["llm"]` に実クライアントを差し込む。

```bash
export AZURE_OPENAI_API_KEY=<key>
python3 mrb_live_llm_check.py <repo>   # ハーネスは repo の外に置く
```

### 🔴 出荷される経路では LLM が注入されない（実測・2026-08-20）

`src/api/server.py:70` が `ClaimConsistencyCheckAgent` に渡す config は
`max_retry` / `case_service` / `lv3` / `limits` の 4 キーで、**`llm` を含まない**。
`src/` のどこにも `BaseLLM` 実装を構築する箇所は無い。実測:

```
server.py が渡す config に 'llm' キー: False
config.get('llm') -> None
```

`StructureDocsNode(llm=None)` / `Lv2CrossDocNode(llm=None)` となり、`call_structured()` は
`'NoneType' object has no attribute 'complete'` を `LLMInvocationError` に正規化する。
呼び出し側は設計 §8 に従いこれを捕捉して Lv1 のみで所見を構成するため、
**例外も警告も出ないまま Lv1 専用として動き続ける**。`generation_mode: "llm"` は出荷経路では成立していない。

🔴 **`config/config.yaml` の `llm: {temperature: 0.0}` は罠になりうる。** AgentRegistry 経由の配備では
config.yaml の内容がそのまま `Graph(config=...)` に渡る（マニフェストとは別に runtime
config が読み込まれ、辞書のまま渡される）ため、
`config.get("llm")` が **`BaseLLM` ではなく dict** を返す。実測:

```
config.yaml の llm ブロック(dict) -> LLMInvocationError: 'dict' object has no attribute 'complete'
```

これも同じ縮退経路に落ちるので、standalone / registry のどちらでも**静かに Lv1 専用**になる。
配線には `requires.secrets` への API キー宣言が要り `agent.yaml` は編集禁止のため所見として留置する。
