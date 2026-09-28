# Template Design Specification — INS-C2-906 ClaimConsistencyCheckAgent

> Proposal overview: `docs/01_proposal.md`
> 本設計書は提案段階で未確定だった論点（トランザクション境界 = §7、PHI 蓄積境界 = §6、Lv3 算出定義 = §5-3）を確定させる。

## 1. Position in AgentCore Architecture

- **Agent Class**: `ClaimConsistencyCheckAgent`
- **L1 Base**: AgentBaseGraph（**L1 Base = AgentBaseGraph を直継承**。AutonomousBaseGraph は不採用 — 自律ループ不要の固定パイプライン）
- **Composition Pattern**: main スロットに `GraphNode` サブクラス（`ClaimWorkflowGraphNode`）を置き、内側に多段ドメインワークフロー（`src/graph/domain_workflow_graph.py`）を持つ Cat 2 標準構成
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 禁止 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override）
  - Graph: composition（`register_nodes()` でノード差し替え。外側 `add_edges()` は override しない）

## 2. Architecture Overview

### 2-1. 外側グラフ（固定5ノード backbone）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (RETRY, max 3)
                                  pre_process
```

initialize / finalize は `super().register_nodes()` が注入する framework 既定ノード。テンプレートは 3 スロットを埋める。

### Node Configuration

| Node | Class | Responsibility | Input State | Output State | Inherits |
|------|-------|---------------|-------------|--------------|----------|
| initialize | (framework) | セッション初期化 | — | — | InitializeNode (default) |
| pre_process | `ClaimIntakeNode` | mode 判定・入力スキーマ検証・書類ごとの正規化（doc_type 推定・段階要約）・S-2 入力ゲート（PII 残存検査） | `user_input`, `input_context` | `mode`, `case_id`, `documents`, `contract`, `validated_input`, `status` | FunctionNode |
| main | `ClaimWorkflowGraphNode` | 内側ドメインワークフロー（§3）の起動・結果マージ | `validated_input`（mode/case_id/documents/contract/claimant_key/feedback payload を内包 — §3） | `result`, `lv1_flags`, `lv2_flags`, `lv3_result`, `revision`, `revision_diff`, `checked_items`, `persistence_status`, `status` | GraphNode |
| post_process | `OpinionComposeNode` | 査定所見ドラフト統合（Markdown+JSON）・断定文言抑止・S-3 出力ゲート | `result`, `lv*_flags`, `checked_items` | `output`（最終応答）, `status` | FunctionNode |
| finalize | (framework) | 出力射影・終了処理 | — | — | FinalizeNode (default) |

### 2-2. 3 モード構成（single-entry 整合）

単一の `ClaimConsistencyCheckAgent` グラフを `mode` で分岐する。`src/api/server.py` が 3 HTTP ルートを受け、いずれも同一グラフを invoke する（複数グラフを立てない — AgentCore single-entry 設計と整合）。

| HTTP ルート | mode | 動作 | DB 書き込み |
|---|---|---|---|
| `POST /ingest` | `ingest` | 既存の確定査定表・事例集（CSV/Excel 由来の行データ）を列マッピング＋正規化＋最小化して投入 | 査定基準KB / `assessment_cases` へ INSERT（行単位・§7-3） |
| `POST /invoke` | `assess`（既定） | Lv1→Lv2→Lv3 チェック＋所見ドラフト生成。**`case_id` は任意指定**: 無指定→新規ケース採番（`revision=1`）、既存 `case_id` 指定→実在検証の上、送られた書類セットでフル再実行し**同一ケースの新リビジョン（`revision=n+1`）**として記録（§2-3） | 暫定レコード INSERT（`final_verdict='needs_review'`・1 リビジョン = 1 行）→ `case_id`＋`revision` 返却 |
| `POST /feedback` | `feedback` | `case_id`＋査定員の確定判定・フラグ単位の valid/dismissed を受領。既定で**最新リビジョン**に適用（任意で `revision` 指定可） | 該当リビジョン行 UPDATE（冪等・§7-2） |

### 2-3. ケースリビジョン（追加書類の再チェックを同一 `case_id` で受ける）

実務では「初回チェック後に診断書が追加で届く」が頻発する。追加書類の再チェックは**新規ケースではなく同一 `case_id` の新リビジョン**として受け、ケース単位のチェック履歴を 1 本のスレッドに繋ぐ。

- **契約**: AG は書類本文を保存しない（PHI 最小化・§6-3）ため、再チェック時は**クライアントが書類一式（既存＋追加）を再送する**。ケーススレッドが繋ぐのは**チェック結果（コード化フラグ）の履歴であり、書類ではない** — この点は API 契約・応答文言・`docs/07_operation_guide.md` に明記する（§13）
- **リビジョン間差分の提示（本機能の価値の核）**: 新リビジョンの所見に「前回リビジョンとの差分」節を追加 — **解消された矛盾フラグ**（前回あり→今回なし）/ **新規フラグ** / **継続フラグ**。前回結果はコード化フラグとして保存済みなので、書類本文なしで差分計算できる。**差分の同定は `flag_key`（§5-2 で定義する決定論的フラグ恒等キー）で行う** — LLM が発番する実行ごとの `flag_id` はリビジョン間で安定しないため、差分・dismissed 照合の基準にしない
- **dismissed 済みフラグの再検出**: 前回リビジョンで査定者が dismiss したフラグ（`flag_key` 一致）が再検出された場合、「前回査定者が dismiss 済み」を注記して提示する（**再考の材料として出す — 自動抑制はしない**。書類が変わった以上、同種フラグでも根拠が変わり得るため）
- **存在しない `case_id` 指定はエラー**（`unknown_case_id`）— feedback と同じ検証
- **DB 到達不能時の再チェックは fail-fast**（§8）: `case_id` の実在検証も前回フラグ取得もできないため、チェックを走らせず `status=error`（`case_thread_unavailable`）で「復旧後に再実行」を案内する。未記録の所見を返してスレッドに穴を開けるより、契約の一貫性（成功応答には必ず `case_id`＋`revision` が載る）を優先する

mode は `input_context.mode` で受け、pre_process が検証する（不正値は `status=error`）。

### 2-4. Marketplace write-mode scope と read-only 配置

AGENTIC STAR Marketplace は任意の検証済み外部利用者が呼ぶ共有入口であり、書き込み権限を表す個別 scope を
runner から受け取れない。このため repo 直下の `cli.py` は貼り付け JSON の `mode` を信用せず、常に
`assess` に固定する。`ingest` / `feedback` は Marketplace から到達不能であり、認証・認可と業務上の
書き込み権限を持つ FastAPI 経路だけに残す（Marketplace 入口で得られる信頼レベルでは
書き込みを認可できないため、書き込みモードはそこから到達不能にするという原則に従う）。

ただし assess 自体も通常は `persist_case` で履歴を INSERT する。そのため Marketplace の
`case_service` は seed 済み repository を内側に持つ read-only facade とし、read 系だけを委譲、write 系は
`ReadOnlyStoreError` で明示拒否する。`persist_case` はこれを DB 障害と混同せず、再試行せずに
`persistence_status="read_only"` を返す。したがって Marketplace の母集団は同梱サンプルを読み取れるが、
発呼によるリビジョン蓄積・母集団成長・feedback は発生しない。これは制限を no-op で隠さず、応答にも
読み取り専用であることを表示する設計である。

## 3. 内側グラフ設計（`src/graph/domain_workflow_graph.py`）

`ClaimCheckWorkflowGraph` は `BaseGraph` を直継承（カスタムトポロジ・7 abstract 全実装）。**分岐関数（`route` / mode 分岐）は必ず自 schema（`ClaimWorkflowState`）で注釈する** — `AgentState` 注釈だとカスタム欄が射影で消え誤 route する（実機確定事項）。

```
START → parse_payload → mode_router ┬─(assess)──→ load_case_thread → structure_docs → normalize_entities → lv1_deterministic
                                    │                 → lv2_cross_doc → lv3_anomaly → persist_case → compose_findings → END
                                    ├─(ingest)──→ parse_rows → map_columns → minimize_normalize → store_records → END
                                    └─(feedback)→ validate_case → apply_feedback → END
```

| 内側ノード | 責務 | 備考 |
|---|---|---|
| `parse_payload` | 内側グラフの先頭ノード。`user_input`（外側 `extract_input()` が渡す JSON 文字列）を deserialize し、`mode` / `case_id`（任意） / `documents` / `contract` / `claimant_key` / `feedback_payload` を `ClaimWorkflowState` の各フィールドへ展開する | 内側は `user_input(str)+ctx` のみ受領するため、**文字列→TypedDict の復元をこのノードが一元的に担う**。parse 失敗は `status=error` |
| `mode_router` | mode 値で 3 経路へ分岐（add_conditional_edges） | 分岐関数は `ClaimWorkflowState` 注釈 |
| `load_case_thread` | assess 経路の先頭。入力 `case_id` 指定時（= 再チェック）: 実在検証（無ければ `status=error`・`unknown_case_id`）→ 前回リビジョンのコード化フラグ＋フラグ単位 verdict（dismissed 含む）を `prior_flags` に読み込み、`revision = max(revision)+1` を**暫定**割り当て（確定は `persist_case` の INSERT 成功時 — §7-1 (4)）。無指定時（= 新規。`case_id` は pre_process が UUID 採番済み — §7-1）: `revision=1`・DB 読みなし | fail-fast（LLM 実行前に unknown_case_id を弾く）。再チェック時の DB 到達不能は新規扱いにフォールバックさせず `status=error`（`case_thread_unavailable` — §2-3/§8） |
| `structure_docs` | 書類ごとに LLM で要約→事実エンティティ抽出（事故日/初診日/傷病部位/損傷箇所/金額/対象/当事者ロール）。**大容量書類は書類単位→セクション単位の段階要約**でトークン上限回避 | 1 書類 = 1 LLM 呼び出し単位（書類間でコンテキストを混ぜない） |
| `normalize_entities` | 日付/金額/カテゴリの正規化（和暦→ISO、金額単位、傷病カテゴリタグ化）。エンティティごとに **出典ポインタ（doc_id + 抜粋）** を保持 | Lv2 の根拠提示の材料 |
| `lv1_deterministic` | 決定論チェック: 請求額×限度額×免責 / 事故日×契約期間 / 必須項目欠落 / 書類内の金額合算整合。**LLM 不使用（純計算）** | 実行した検査項目を `checked_items` に全件記録（白判定含む） |
| `lv2_cross_doc` | 抽出済みエンティティを書類横断で突合し記述矛盾を検出（例: 事故日↔初診日、事故態様記述↔損傷箇所記述、請求対象↔傷病部位）。**テキスト整合性に限定 — 医学的妥当性・物理因果評価は射程外**。全フラグに根拠（両書類の該当抜粋＋矛盾理由）必須・「記述矛盾の可能性・要確認」形式で断定しない | temperature 0.0 |
| `lv3_anomaly` | `assessment_cases` の `policy_type × coverage_item` 母集団（**確定済みレコード限定 — §5-3**）に照らし異常度算出＋反復請求パターン照合＋類似事例取得。**現請求 INSERT 前に読む**（自己包含バイアス回避） | DB 到達不能時は縮退（§8） |
| `persist_case` | `revision ≥ 2` のとき、まず**決定論的な差分計算**（純関数 `compute_revision_diff(prior_flags, current_flags)` — `flag_key` 突合・LLM 不使用）で `revision_diff`（解消/新規/継続、dismissed 済み再検出注記）を算出。次に最小化・匿名化済みレコードを **(case_id, revision) の新規 1 行として** INSERT（§6・§7-1）。一意制約衝突時は最新リビジョンのフラグを再読込 → `revision` 再割当 → **差分を新しい前任リビジョン基準で再計算** → 1 回だけ再 INSERT（§7-1 (4)。差分計算は純関数なので再計算に LLM 再実行は不要）。失敗しても後続をブロックしない（内部 try/except → `persistence_status='failed'`） | ベストエフォート書き込み。INSERT 成功後に S-4 `case_revision_created`（差分サマリ件数付き）を発火 — 差分は本ノードで確定済みのため件数を載せられる |
| `compose_findings` | Lv1/Lv2/Lv3 の結果＋checked_items＋追加照会事項＋`revision_diff`（`persist_case` が確定済み）を構造化 findings に統合し `get_output()` へ | 所見の文章化は外側 post_process。差分の**計算**はしない（表示整形のみ — 計算は persist_case §7-1） |
| `parse_rows` / `map_columns` / `minimize_normalize` / `store_records` | ingest 系: 行パース → LLM 列マッピング（非定型列構成の吸収）→ PII/医療情報の最小化・カテゴリ化 → KB/DB 投入。行単位の成否を集計 | 失敗行を分離し、行単位で再実行できる |
| `validate_case` / `apply_feedback` | feedback 系: `case_id` 実在検証（無ければ error）→ `final_verdict`/フラグ単位 verdict/`assessor_edited` を UPDATE（冪等） | §7-2 |

**GraphNode 契約（外側 `ClaimWorkflowGraphNode`）:**
- `get_subgraph()`: 内側グラフを `__init__` でキャッシュして返す（毎回新規生成禁止 — 実機確定事項）
- `extract_input()`: 外側 State の `validated_input`（mode・documents・contract・feedback payload を JSON 直列化した文字列, `default=str`）を渡す — 内側は `user_input(str) + ctx` のみ受領するため
- `merge_output()`: 内側 `get_output()` の findings を外側 State の変更キーのみにマップ（full state 返却禁止）
- `error_strategy = "handle"`: 内側の予期せぬ例外は `on_subgraph_error()` で `status=error`＋利用者向けメッセージへ graceful degradation（査定業務中断を防ぐ）。エラー内容は S-4 監査ログへ
- `propagate_hitl = False`（HITL 不使用 — §4-3）

## 4. State Definition

### 4-1. 外側 State（`src/schemas/state.py` — AgentState 継承 flat TypedDict）

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `"assess"` / `"ingest"` / `"feedback"` | ✅ |
| `case_id` | `str` | 受付採番 ID（UUID v4・pre_process で採番）。**assess の再チェック時・feedback 時は入力値**（§2-3） | ✅(assess/feedback) |
| `revision` | `int` | ケース内リビジョン番号（新規=1、再チェック=n+1。内側 `load_case_thread` が割当） | ✅(assess) |
| `revision_diff` | `dict` | 前回リビジョンとの差分（解消/新規/継続フラグ・dismissed 再検出注記。`revision=1` は空） | — |
| `documents` | `list[dict]` | 書類ごとの正規化済みテキスト＋doc_type（**仮名化トークン適用後** — §6-1） | ✅(assess) |
| `contract` | `dict` | 契約内容（`coverage_limit`, `deductible`, `policy_start`, `policy_end`, `policy_type`, `coverage_items[]` — 構造化値。pre_process が入力から検証・正規化して State に置き、`validated_input` にも内包して内側へ渡す） | ✅(assess) |
| `claimant_key` | `str` | server.py がトークン化と同時に導出する一方向仮名化キー（§6-3。生識別子はグラフに入らない） | — |
| `validated_input` | `str` | 内側グラフへ渡す JSON 直列化ペイロード | ✅ |
| `lv1_flags` / `lv2_flags` | `list[dict]` | フラグ（`flag_id`, `level`, `kind`, `evidence[]`, `severity`） | — |
| `lv3_result` | `dict` | 異常度（`anomaly_score`, `percentile`, `population_n`, `similar_cases[]`, `degraded`) | — |
| `checked_items` | `list[dict]` | 実行した全検査項目と結果（白判定含む） | — |
| `persistence_status` | `str` | `"ok"` / `"failed"` / `"skipped"` / `"read_only"` | — |
| `ingest_summary` / `feedback_result` | `dict` | 各モードの結果サマリ | — |
| `result` / `output` | `dict` | 統合 findings / 最終応答 | — |

**State Constraints (mandatory):**
- Flat TypedDict のみ（primitives + JSON-serializable 型）
- JWT・API キー・資格情報・DB 接続ハンドルを State に置かない
- InvocationContext は `config["configurable"]` 経由のみ（State 非格納）
- Pydantic モデル・dataclass・任意 Python オブジェクト禁止（msgpack 非互換）
- **氏名・連絡先・証券番号等の生 PII は State に置かない**（§6-1 の仮名化トークンのみ）

### 4-2. 内側 State（`ClaimWorkflowState` — 自 schema・flat TypedDict）

route / get_output / mode 分岐関数は必ず本 schema で注釈する（`AgentState` 注釈だと以下のカスタム欄が射影で消え誤 route — §3）。制約は §4-1 の State Constraints と同一（生 PII 禁止・msgpack 可能型のみ）。

| Field | Type | Purpose | 使用モード |
|-------|------|---------|-----------|
| `mode` | `str` | `"assess"` / `"ingest"` / `"feedback"`（`parse_payload` が展開） | 全 |
| `case_id` | `str` | ケース ID（新規は外側採番値・再チェックは入力値） | assess / feedback |
| `revision` | `int` | リビジョン番号（`load_case_thread` が暫定割当・INSERT 成功で確定 — §7-1 (4)） | assess |
| `prior_flags` | `list[dict]` | 前回リビジョンのコード化フラグ＋フラグ単位 verdict（dismissed 含む。`revision=1` は空） | assess |
| `revision_diff` | `dict` | 前回リビジョンとの差分（解消/新規/継続・dismissed 再検出注記。`persist_case` が確定 — §3） | assess |
| `documents` | `list[dict]` | 仮名化トークン適用済みの書類別テキスト＋doc_type | assess |
| `contract` | `dict` | 契約内容（§4-1 と同 schema） | assess |
| `claimant_key` | `str` | server.py 導出の一方向仮名化キー（§6-3） | assess |
| `entities` | `list[dict]` | 書類別の正規化済み事実エンティティ＋出典ポインタ（doc_id＋抜粋） | assess |
| `lv1_flags` / `lv2_flags` | `list[dict]` | 各レベルの検出フラグ（`flag_id`, `flag_key`, `kind`, `evidence[]`, `reason`, `severity` — §5-2） | assess |
| `lv3_result` | `dict` | 異常度（`anomaly_score`, `percentile`, `population_n`, `similar_cases[]`, `degraded`） | assess |
| `checked_items` | `list[dict]` | 実行した全検査項目と結果（白判定含む） | assess |
| `persistence_status` | `str` | `"ok"` / `"failed"` / `"skipped"` / `"read_only"` | assess / ingest |
| `ingest_rows` | `list[dict]` | 取り込み対象行（列マッピング後・最小化後） | ingest |
| `ingest_summary` | `dict` | `n_ok` / `n_failed`（行番号＋理由）/ 列マッピング結果 / `dry_run` | ingest |
| `feedback_payload` | `dict` | `case_id`・`revision`（任意）・`final_verdict`・フラグ単位 verdict・`assessor_edited` | feedback |
| `feedback_result` | `dict` | UPDATE 結果（対象リビジョン・冪等判定） | feedback |
| `status` | `str` | `AgentStatus` 値（`"success"` / `"error"` 等） | 全 |

### 4-3. HITL 判断: 不使用（`hitl.enabled: false` / `memory_enabled: false`）

査定者のレビューは**グラフ外の非同期業務**として発生する（所見ドラフトを持ち帰り検討 → 後日 `/feedback`）。invoke 中に人間応答を待つ場面がないため、D6 `interrupt()` は不要。人間参加は `case_id` を跨ぐ `/invoke` → `/feedback` の 2 フェーズ・ライフサイクルで実現し、追加書類到着時の再チェックは同一 `case_id` の**ケースリビジョン**（§2-3）で受ける。checkpointer を持たないことで、**checkpoint DB への PII/医療情報残存リスクそのものを排除**できる（§6-2）— これが本設計で HITL 非採用を選ぶ最大の理由。将来対話 refine が要件化した場合は checkpoint 内容の PHI 精査を再設計条件とする。

## 5. 3層バリデーション仕様

### 5-1. Lv1 — 決定論的チェック（内部・LLM 不使用）

| 検査 | ロジック |
|---|---|
| 金額整合 | `claimed_amount ≤ coverage_limit`、免責額控除の整合、明細合算 = 請求合計 |
| 期間整合 | `policy_start ≤ incident_date ≤ policy_end` |
| 必須項目 | doc_type 別必須フィールド（請求書=金額/対象、診断書=傷病名/初診日 等）の欠落検出 |

すべて純計算。結果は成否に関わらず `checked_items` に記録する（§10 ユーザビリティ①）。

### 5-2. Lv2 — 書類間記述矛盾抽出（LLM クロスドキュメント）

- 入力: `normalize_entities` が出典ポインタ付きで整理した書類別エンティティ集合
- 突合ペア例: 事故日(事故報告)↔初診日(診断書) / 事故態様記述↔損傷箇所記述 / 請求対象↔傷病部位・修理対象 / 当事者ロールの食い違い
- 出力契約: 各フラグ = `{flag_id, flag_key, kind, evidence: [{doc_id, quote, field}], reason, severity}`。**根拠（両書類の該当抜粋）が無いフラグは compose 段で破棄**
- **フラグ恒等キー `flag_key`（リビジョン間差分・dismissed 照合の基準）**: `flag_id` は実行ごとの通し番号で安定しないため、**決定論的に導出する `flag_key` を別途持つ**。定義: `flag_key = f"{kind}:{sorted(正規化済みアンカー)}"` — `kind` は**固定タクソノミのコード値**（Lv1: `LV1_AMOUNT_OVER_LIMIT` / `LV1_INCIDENT_OUT_OF_TERM` / `LV1_MISSING_FIELD` 等の enum、Lv2: `LV2_DATE_CONFLICT` / `LV2_BODYPART_MISMATCH` / `LV2_SUBJECT_MISMATCH` / `LV2_NARRATIVE_INCONSISTENCY` 等の enum。LLM には**この enum への構造化出力を強制**し、enum 外の kind は破棄）、アンカーは矛盾に関与したエンティティ参照の正規形（例: `("accident_report.incident_date", "medical_cert.first_visit_date")` — doc_type と正規化フィールド名のペア。書類の並び順・quote 文言には依存しない）。同じ論理矛盾は書類再送後も同一 `flag_key` に落ちることを設計契約とし、テストで検証する（§13-1）
- 射程: テキスト整合性のみ。医学的妥当性・物理因果は評価しない（プロンプトで明示的に禁止し、専門判断が必要な場合は「専門家確認を推奨」タグを付す）
- LLM 設定: temperature 0.0・構造化出力（JSON schema）

### 5-3. Lv3 — 異常度スコアリング（算出定義の確定）

- **母集団**: `assessment_cases` を `policy_type × coverage_item` で切り出し、**かつ確定済みレコード（`final_verdict IN ('paid','partial','denied')`）に限定**する。`needs_review`（受付直後の暫定）・`investigating` は分布から除外 — 未確定請求が母集団を汚染すると「精査対象そのものが基準になる」バイアスが生じるため。`/ingest` で投入する既存確定査定表は確定済みなので、導入初日からこの条件で母集団が成立する（コールドスタート整合）。医療系と物損系の分布を混在させない。**さらに現請求と同一 `case_id` の全リビジョンを母集団・類似事例の両方から除外**する（自己包含バイアス回避の拡張 — 自ケースの過去リビジョンを「過去事例」として参照しない）
- **算出**（対象変数: `claimed_amount`, `treatment_or_repair_period`）:
  - ロバスト z-score: `z = (x − median) / (1.4826 × MAD)`（MAD=0 のときは IQR ベースへフォールバック、それも 0 なら「分布退化・スコア無効」）
  - 分位点: 母集団内パーセンタイル
- **最小母集団**: `population_n < min_population_n`（config 既定 30）の場合はスコアを出さず `degraded: "insufficient_population"` を明示（誤った統計的印象を与えない）
- **反復請求パターン**: `claimant_key`（§6-3 の一方向仮名化キー）が一致する過去レコードの **distinct `case_id` 件数**・直近期間内の頻度を提示（**リビジョンを重複カウントしない**。件数事実のみ・不正断定はしない）。※反復照合のみ未確定レコードも対象に含める（「同一人物の請求が既に複数受付済み」という事実提示が目的のため）
- **類似事例**: 同母集団・傷病/損傷カテゴリ近傍の**確定済みレコード**を上位 N 件参考提示
- **提示形式**: スコア単体でなく「母集団 n=?、中央値 ?、本件 ? パーセンタイル」の文脈付き（§10 ユーザビリティ②）

## 6. PII / PHI 取り扱い設計（S-2 ゲート方針・蓄積境界）

### 6-1. 参照ID（仮名化トークン）方式 — グラフ内に生 PII を入れない

- `src/api/server.py`（API アダプタ層）が invoke 前に `shared.security.pii_detector`＋ドメイン辞書（証券番号形式等）で直接識別子（氏名・連絡先・住所・証券番号・個人特定医療識別子）を検出し、**リクエストスコープの PIIVault（メモリ内 map）でトークン化**（`[PERSON_1]`, `[POLICY_NO_1]` …）してからグラフへ渡す
- **`claimant_key` はトークン化と同時に server.py で導出する**（`HMAC-SHA256(salt, 被保険者識別子)` — §6-3）。リクエストごとに揮発する PIIVault トークンからはリクエスト横断の同一性が取れないため、安定キーの導出は生識別子を見られる唯一の層＝アダプタ層で行い、グラフには導出済みキーのみを渡す（グラフ内は生識別子を一切見ない）
- グラフ内の全ノード戻り値・State・S-4 監査ログにはトークンのみが流れる。**resolve（トークン→実値の復元）は server.py の応答整形でのみ行い、グラフ内ではどのノードも resolve しない**（Cat2 センシティブ値の実機確定ルール）
- 傷病・損傷の記述内容（medical content）は Lv2 チェックに必要なためトークン化対象外だが、直接識別子と切り離された状態でのみ扱う。checkpointer 非使用（§4-3）のため State はプロセスメモリ限りで永続化されない

### 6-2. S-2 入力ゲート（`ClaimIntakeNode._extra_security_gate_input()`）

- framework 既定の PII スキャン（`@final` `_security_gate_input()` — 触らない）に加えて:
  - **仮名化検証**: server.py のトークン化を突破した生 PII パターン（電話番号・メール・証券番号形式）が `documents` に残存していないか再スキャン → 検出時は fail-closed（`SecurityViolationError`）
  - 入力サイズ上限（書類数・総文字数）検査
  - mode 値・必須ペイロードのスキーマ検査
- GraphNode（main）の S-2/S-3 は意図的な no-op パススルー（上流の pre_process / 下流の post_process がゲートを担う）

### 6-3. 蓄積境界（`assessment_cases` — 顧客テナント内限定）

- **保管主体**: 顧客テナントごとの専用 SQLite ファイル（repository factory が `SecretProvider.require("ASSESSMENT_DB_PATH")` からパスを解決）。顧客横断でファイルを共有しない
- **格納するもの**: コード値・数値のみ（`case_id`, **`revision`**, `policy_type`, `coverage_item`, `incident_type` タグ, `injury_category` タグ, `treatment_or_repair_period`, `claimed_amount`, `consistency_flags` の種別コード＋**`flag_key`（§5-2 — リビジョン間照合キー）**＋書類参照ポインタ, `anomaly_score`, `final_verdict`, `assessor_edited`, フラグ単位 verdict）。**一意キーは `(case_id, revision)`**（§2-3 — Proposal §4-3 の `case_id` PK をリビジョン対応で拡張。§12 DDR に記録）
- **書類本文は格納しない**: 再チェック（新リビジョン）時はクライアントが書類一式を再送する契約（§2-3）。ケーススレッドが繋ぐのはコード化されたチェック結果の履歴のみ
- **格納しないもの**: 氏名・連絡先・証券番号・個人特定医療識別子・書類本文・**AG 生成所見の自由記述本文（`ag_opinion` 相当）**
  - Proposal の「AG の所見と人間の確定判定の両方を蓄積」は、**所見のコード化表現（`consistency_flags` 種別コード＋`anomaly_score`）＋フラグ単位 verdict＋`assessor_edited`** で満たす。学習ループ（AI 所見が外れやすいパターンの可視化）はこのコード化表現で機能し、自由記述本文の保管は不要 — 蓄積能力の約束と PHI 最小化は矛盾しない
- **`claimant_key`**: 反復請求検出（Proposal §4-3 記載機能）のため、被保険者識別子を `HMAC-SHA256(salt, claimant_id)` で一方向仮名化したキーを格納する。**導出は server.py（アダプタ層）で行い**（§6-1 — グラフは生識別子を見ない）、salt はテナント別 secret（`CLAIMANT_HASH_SALT` — server.py が secrets provider 経由で取得。`os.environ` 直読み禁止は同様に適用）。生識別子は保存しない。※Proposal のスキーマ表に無いカラムの設計時追加 — §12 Design Decision Record に記録
- **保持・削除**: `retention_days`（config 既定 1825 = 5年・顧客要件で変更可）。日次 purge SQL を `docs/07_operation_guide.md` に添付し顧客運用で実行（agent 内に cron を持たない）。`/feedback` 経由の個別削除要求（`action: "delete"`）で単一レコード物理削除に対応（データ主体の削除権行使ルート）

### 6-4. S-3 出力ゲート（`OpinionComposeNode._extra_security_gate_output()`）

- framework 既定の credential スキャン（`@final` — 触らない）に加えて:
  - **断定文言抑止**: 支払可否の確定表現（「支払対象外です」「不正請求です」等の禁止語リスト）を検査し、検出時は「要確認」表現へ降格（ブロックでなく降格 — 所見は必ず返す）
  - 出力ドラフトへの PII 残存再スキャン（トークン化漏れの最終防衛線）
  - 根拠なしフラグ（evidence 空）の除去検証

## 7. トランザクション境界

### 7-1. assess モード: 「所見返却」と「履歴蓄積」の整合

**方針: 所見生成（読み取り系）と蓄積（書き込み系)を分離し、蓄積はベストエフォート・所見は必ず返す。**

1. 新規ケースの `case_id` は pre_process で採番（UUID v4）— DB 採番に依存しない（DB 停止時も所見返却可能）。リビジョン番号のみ DB 読みに依存（`load_case_thread`・§3）
2. `lv3_anomaly`（母集団読み取り）→ `persist_case`（INSERT）の順序を固定 — 現請求を自分の母集団に含めない（同一 `case_id` の過去リビジョンも除外 — §5-3）
3. INSERT は **単一行・単一文**（`assessment_cases` の **1 リビジョン = 1 行**にチェック結果コードまで含めて書く）— マルチテーブル分散トランザクションを持たない。これが「チェック結果と履歴の整合」の実現手段: 結果とレコードが同一行なので部分書き込み状態が存在しない（**リビジョン導入後も原則不変** — 過去リビジョン行は追記のみで更新しない）
4. `(case_id, revision)` 一意制約が同時再チェックの衝突を検出する: 同一 `case_id` への並行 INSERT が同じ `revision` を取った場合、後着は一意制約違反 → `persist_case` 内で **(a) 最新リビジョンのフラグ再読込 → (b) `revision` 再割当 → (c) `revision_diff` を新しい前任リビジョン基準で再計算（`compute_revision_diff` は `flag_key` 突合の純関数 — LLM 再実行不要）→ (d) 再 INSERT** を 1 回だけ実行（それでも失敗なら `persistence_status='failed'`）。**古い基準で計算した差分をそのまま書かない** — `load_case_thread` の `prior_flags`/`revision` は暫定値であり、確定は INSERT 成功時
5. INSERT 失敗時: `persistence_status='failed'` を応答に明示し、所見ドラフトはそのまま返す。応答に「このリビジョンは蓄積されていないため `/feedback` は受理されない。記録するには同一書類で再実行（新リビジョンとして記録される）」を明記（silent failure にしない — §10 ユーザビリティ⑤）
6. `case_id` 無指定の再実行は新規 `case_id` の新規ケース。**同一ケースへの追加書類・訂正書類は `case_id` 指定の新リビジョン**として受ける（§2-3）。別ケースとしての重複請求の検知は Lv3 反復請求パターンが担う

### 7-2. feedback モード: 冪等 UPDATE

- `case_id` 必須。存在しない `case_id` は `status=error`（`unknown_case_id`）
- **適用先リビジョン**: 既定で最新リビジョン。任意で `revision` 指定可（指定リビジョンが存在しなければ `unknown_revision` エラー）
- UPDATE は確定系フィールド（`final_verdict`, フラグ単位 verdict, `assessor_edited`）のみを対象とする単一行（= 対象リビジョン行）UPDATE。同一ペイロードの再送は同一最終状態に収束（冪等）
- 相異なる確定値の再送は last-write-wins＋`updated_at` 更新（査定のやり直しを正として受理）。監査は S-4 の `feedback_applied` イベントで追跡

### 7-3. ingest モード: 行単位コミット・再投入安全

- 全行の all-or-nothing はとらない（数千行の 1 行エラーで全滅させない）。**行単位 INSERT・成否集計**を応答（`n_ok`, `n_failed`, 失敗行番号＋理由）
- 各行に `source_row_hash`（正規化後行内容のハッシュ）を持たせ、再投入時は同一ハッシュ行をスキップ（重複投入安全）
- 列マッピング結果（どの列→どのカラム）は投入前レビューのため応答に含める（`dry_run: true` で投入なしのマッピング確認のみ実行可能 — §10 ユーザビリティ④）

## 8. エラー / フォールバック設計

| 障害 | 挙動 | 利用者への提示 |
|---|---|---|
| LLM 呼び出し失敗（Lv2/structure） | リトライ（ConnectionPolicy）→ 失敗時は Lv1 結果のみで所見を構成 | 「Lv2 記述矛盾チェックは未実施」を checked_items と応答に明示 |
| `assessment_cases` DB 到達不能（新規ケース） | Lv3 スキップ（`degraded: "db_unavailable"`）・persist も skipped | 「異常度・類似事例は今回参照不可」を明示。Lv1+Lv2 は返す |
| `assessment_cases` DB 到達不能（`case_id` 指定の再チェック） | **fail-fast**: 実在検証・prior_flags 読込が不可能なためチェックを実行せず `status=error`（`case_thread_unavailable`）。新規ケースへのフォールバックもしない（§2-3 — 成功応答には必ず `case_id`＋`revision` が載る契約を維持） | 「ケーススレッド参照不可のため再チェック未実施。復旧後に再実行。至急の場合は case_id 無指定の新規チェックも可（スレッドには繋がらない）」を返す |
| 母集団不足 | スコア非表示（§5-3） | `insufficient_population`（n 値付き）を明示 |
| 内側グラフ予期せぬ例外 | `error_strategy="handle"` → `status=error`＋原因区分 | 部分結果があれば checked_items と共に返す |

原則: **どの障害でも「何をチェックできて何ができなかったか」を必ず返す**（無言の縮退をしない）。

## 9. Security Model 適合

### S-1（Trust Gate）

| ノード | `required_trust_level` | 根拠 |
|---|---|---|
| `ClaimIntakeNode`（pre_process） | `VERIFIED_EXTERNAL` | 請求書類（PII 含む業務データ）を扱う標準業務操作 |
| `ClaimWorkflowGraphNode`（main） | `VERIFIED_EXTERNAL` | 同上＋DB 読み書き |
| `OpinionComposeNode`（post_process） | `VERIFIED_EXTERNAL` | 所見出力整形 |
| 内側全ノード（`structure_docs`〜`apply_feedback`） | `VERIFIED_EXTERNAL` | 全 FunctionNode でクラス本体に明示宣言（暗黙 ANONYMOUS 継承は不可） |

有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` のみ（enum 外の値は import 時 AttributeError）。ingest も顧客管理者の認証済み操作として `VERIFIED_EXTERNAL`（デプロイ経路を INTERNAL に限定したい顧客は config でなくゲートウェイ側ポリシーで絞る — テンプレは過剰権限要求をしない）。

### S-2 / S-3

§6-2 / §6-4 のとおり。FunctionNode は `_extra_security_gate_input()` / `_extra_security_gate_output()` フックのみで拡張し、`@final` の `_security_gate_input()` / `_security_gate_output()` は override しない（import 時 TypeError）。GraphNode の S-2/S-3 は、前後の FunctionNode がゲートを担うため意図的な no-op パススルーとする。

### S-4（Audit）

各ノードの `execute()` 内で `shared.utils.audit_logger.emit_trace_event()` を最低 1 回発火（`node_start`/`node_complete`/`node_error` は framework が `__call__()` で発火するため重複させない）:

`claim_intake_validated` / `case_thread_loaded` / `claim_docs_structured` / `entities_normalized` / `lv1_check_completed` / `lv2_contradiction_scan_completed` / `lv3_anomaly_scored` / `case_persisted`（成否含む） / **`case_revision_created`**（`case_id`, `revision`, 差分サマリ件数 = 解消/新規/継続の各件数。**`persist_case` が INSERT 成功後に発火** — 差分は同ノードで INSERT 前に確定済み・§3/§7-1） / `findings_composed` / `opinion_draft_composed` / `ingest_batch_completed` / `feedback_applied`。ペイロードはコード値・件数のみ（PII・書類本文・所見本文を載せない）。

### S-5 / Secrets

- `ctx.secrets.require()` または構築時の `SecretProvider.require()` のみ使用（`os.environ` 禁止）。State・ログ・所見に資格情報を載せない
- `config/agent.yaml` `requires.secrets`: `ASSESSMENT_DB_PATH`（テナント別 SQLite ファイルパス）/ `CLAIMANT_HASH_SALT`（§6-3）
- `pyproject.toml` 依存は全て `==` 完全固定（gate-dep-pinning）

## 10. ユーザビリティ設計レビュー（設計判断として反映した修正点）

エンドユーザー = 初期査定担当（1 日数十件の請求を一次スクリーニング）を想定してレビューし、以下を設計に反映した:

1. **指摘ゼロでも「何を検査して白だったか」を返す**（→ `checked_items` を Lv1/Lv2/Lv3 全項目で必須化・所見ドラフト冒頭に「検査カバレッジ」節を置く）。フラグ 0 件の応答が「チェックされていない」のか「チェックして問題なし」なのか区別できないと査定者は次工程へ進めない — 白判定の明示を出力契約に昇格した
2. **矛盾フラグは「箇所と根拠」を原文参照付きで**（→ evidence に両書類の doc_id＋該当抜粋＋矛盾理由を必須化し、根拠なしフラグは S-3 前に破棄）。スコアや種別コードだけでは査定者は書類を最初から読み直すことになり時短効果が消える
3. **誤検知の dismiss を学習シグナルに**（→ `/feedback` を最終判定だけでなく**フラグ単位の valid/dismissed** を受けるスキーマに拡張し、`consistency_flags` に verdict を持たせる）。dismissed の蓄積は「AI 所見が外れやすいパターン」の可視化（Lv2 プロンプト・Lv3 縮退条件の改善入力）になる
4. **入力は自然に用意できる最小形**（→ 書類はテキスト＋doc_type ラベルのみ。doc_type 未指定時は自動推定し、推定結果を応答に明示。ingest は非定型 CSV/Excel を LLM 列マッピングで吸収し、`dry_run` でマッピング結果を投入前確認可能に）。過剰な構造化 JSON を査定者に要求しない
5. **蓄積失敗を silent にしない**（→ `persistence_status` を応答の一級フィールドにし、feedback 不可となる旨と回復手順を文言で返す）
6. **出力は二形式**（→ 人間向け Markdown 所見ドラフト＋システム連携向け構造化 JSON を常に併せて返す。所見は「結論めいた要約」でなく「論点→根拠→推奨アクション（追加照会文案含む）」の順で、査定者がそのまま照会状に転記できる粒度）
7. **追加書類の再チェックは同一ケースのリビジョンで受ける**（→ §2-3。実務で頻発する「初回チェック後に診断書が追加で届く」を、新規ケース扱いでなく `case_id` 指定の再実行＝新リビジョンとして受け、所見に**前回との差分**（解消/新規/継続フラグ・dismissed 済み再検出の注記）を付す。査定者は「追加書類で何が変わったか」だけを見れば済む。書類本文は AG が保存しないため**再チェック時は書類一式の再送が必要**という契約を API 応答・運用ガイドの両方で明示する — ユーザー判断 2026-07-06 反映）

対話型の壁打ち（HITL ループ）は本ユースケースでは価値の中心でない（査定者は書類一式を持って非同期にレビューする）ため採用せず、`case_id` 2 フェーズで人間参加を設計した（§4-3）。

## 11. config/agent.yaml 主要キー（設計値）

```yaml
template_id: INS-C2-906
category: "Cat 2"
base_type: "AgentBaseGraph"    # L1 base class（graph.py の ClaimConsistencyCheckAgent と一致）
module: "src.graph"
class: "ClaimConsistencyCheckAgent"
memory_enabled: false          # checkpointer 不使用（§4-3 / §6-1）
hitl:
  enabled: false
llm:
  model: <deployment-default>
  temperature: 0.0             # Lv2 記述矛盾抽出・列マッピングとも決定論寄り
limits:
  max_documents: 12
  max_total_chars: 400000
lv3:
  min_population_n: 30
  similar_cases_top_n: 5
  repeat_lookback_days: 365
retention:
  assessment_case_retention_days: 1825   # 顧客要件で変更可（§6-3）
requires:
  secrets:
    - ASSESSMENT_DB_PATH
    - CLAIMANT_HASH_SALT
```

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, permissions, credential handle)
- [x] ConnectionPolicy (LLM / DB retry・timeout — §8)
- [x] SecurityViolationError（S-2 fail-closed — §6-2）
- [x] S-2: `_extra_security_gate_input()` — 仮名化残存検査・サイズ/スキーマ検査（§6-2。`_security_gate_input()` は override しない）
- [x] S-3: `_extra_security_gate_output()` — 断定文言降格・PII 再スキャン・根拠なしフラグ除去（§6-4。`_security_gate_output()` は override しない）
- [x] S-4: `emit_trace_event()` — 各 `execute()` 内で最低 1 domain イベント（§9。`node_start`/`node_complete`/`node_error` は発火しない）
- [x] `shared.security.pii_detector`（server.py トークン化＋S-2 再検査）
- [x] `BaseDatabase`＋repository factory（`assessment_cases` — テナント別 SQLite）
- [x] `shared.services.llm.BaseLLM`（コンストラクタ注入 — テストは fake 注入）

### Composition Pattern

- **Pattern**: GraphNode (subgraph)
- **Composition target**: `src/graph/domain_workflow_graph.py` `ClaimCheckWorkflowGraph`（BaseGraph 直継承・mode 3 分岐トポロジ）
- **Error propagation strategy**: **handle** — `on_subgraph_error()` で `status=error`＋部分結果返却へ graceful degradation（§3・§8）。サブグラフのエラーを外側グラフの状態と部分結果へ一貫して反映する

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## 12. Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **A** | 固定パイプライン（自律ループ・自己終了判断が不要）。Cat 2 標準 |
| Composition pattern | Standalone（外側ノードに全ロジック） | GraphNode + 内側 subgraph | **B** | mode 3 分岐×多段チェックは内側トポロジで表現するのが 3 スロット契約と整合 |
| HITL | interrupt() 対話ループ | 非採用（case_id 2 フェーズ＋ケースリビジョン） | **B** | 査定レビューはグラフ外の非同期業務。checkpointer 排除で checkpoint への PHI 残存リスクを構造的に消す（§4-3）。追加書類はリビジョン（§2-3）で受ける |
| PII 防御 | グラフ内でマスキング | API アダプタ層トークン化＋グラフ内はトークンのみ | **B** | ノード戻り値・State・監査ログに生 PII を載せない不変条件を層で保証（§6-1） |
| 蓄積整合 | 分散トランザクション/2PC | 単一行 INSERT＋ベストエフォート分離 | **B** | チェック結果と履歴を同一行に書くことで部分書き込み状態を構造的に排除。書込失敗は所見返却をブロックしない（§7-1） |
| Lv3 反復請求検出 | claimant 識別子を平文格納 | HMAC-SHA256 一方向仮名化キー（**server.py で導出** — グラフ非通過） | **B** | Proposal スキーマに無いカラムの設計時追加。反復請求検出（Proposal 記載機能）に必要な最小のキーを仮名化で持つ（§6-1/§6-3） |
| Lv3 母集団 | 全レコード | 確定済み（paid/partial/denied）限定 | **B** | 未確定請求による分布汚染を排除。/ingest 投入分は確定済みなのでコールドスタートとも整合（§5-3） |
| ingest 整合 | バッチ all-or-nothing | 行単位コミット＋行ハッシュ重複スキップ | **B** | 大量行の運用現実性と再投入安全性（§7-3） |
| エラー戦略 | propagate（fail fast） | handle（graceful degradation） | **B** | 査定現場では「部分結果＋何ができなかったか」が業務継続に必要（§8・§10） |
| 追加書類の再チェック | 新規 `case_id`（都度独立） | 同一 `case_id` の新リビジョン（`(case_id, revision)` 一意・差分提示） | **B** | ケース履歴を 1 本に繋ぎ「追加書類で何が変わったか」を差分で返す。書類非保存の原則は不変（クライアント再送契約）。**ユーザー判断 2026-07-06（選択 (b)）で確定** — §2-3/§7-1 |
| Marketplace 永続化 | assess の履歴 INSERT を許可 | seed 済み DB の read-only facade | **B** | Marketplace caller に共有 DB 書込 scope を付与できないため。write は `ReadOnlyStoreError`、応答は `persistence_status="read_only"` とし、DB 障害の `failed` と区別する（§2-4） |

## 13. テスト・後続ドキュメントのチェックリスト

### 13-0. 実装チェックリスト

- **config/agent.yaml 拡充時（`lv3.*` / `retention.*` / `requires.secrets` 等の追加時）は §11 の設計値との一致を必ず確認する**

### 13-1. `docs/03_test_spec.md` に必ず含めるテスト観点（本設計固有）

- **リビジョン一意性・並行衝突**: `(case_id, revision)` 一意制約・並行 INSERT 衝突時の「フラグ再読込→revision 再割当→差分再計算→再 INSERT」1回リトライ（§7-1 (4) — 古い基準の差分が書かれないこと）・`revision=1`/`n+1` の割当
- **`flag_key` の安定性**: 同一の論理矛盾が書類再送後（quote 文言・書類順序が変わっても）同一 `flag_key` に落ちること・enum 外 kind の破棄（§5-2）
- **リビジョン間差分計算**: `compute_revision_diff` の純関数性（同入力同出力・LLM 非依存）・解消/新規/継続フラグの3分類・dismissed 済みフラグ再検出時の注記付与（自動抑制しないこと）・`revision=1` で差分節が空であること
- **Lv3 除外の拡張**: 母集団・類似事例から同一 `case_id` 全リビジョン除外・反復請求件数の distinct `case_id` カウント（リビジョン重複カウントなし）
- **書類再送契約・fail-fast**: `case_id` 指定再チェックで書類一式が再送される前提の検証（過去書類は AG に存在しないこと = PHI 非保持の裏取り）・`unknown_case_id`/`unknown_revision` エラー・DB 到達不能時の `case_thread_unavailable` fail-fast（チェック未実行・新規フォールバックなし — §8）
- **feedback リビジョン適用**: 既定=最新リビジョン・`revision` 指定時の対象行 UPDATE・冪等性

### 13-2. `docs/07_operation_guide.md` 作成時のチェックリスト

- **【必須・太字記載】保持期間の既定値 5 年（`assessment_case_retention_days: 1825`）は仮置きであり、導入顧客の法務要件（保険業法・個人情報保護法・記録管理規程）に照らして必ず見直すこと**
- 日次 purge SQL（§6-3）と削除権行使ルート（`/feedback` の `action: "delete"`）の運用手順
- 再チェック（新リビジョン）時の**書類一式再送**の運用ルール周知（「ケーススレッドが繋ぐのはチェック結果の履歴であり書類ではない」）
