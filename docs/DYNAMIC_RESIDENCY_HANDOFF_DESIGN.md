# AI Core 可変常駐・引継ぎ・共有資源再利用 — 設計正本候補

**Status: PROPOSED_PR (設計専用PR、main採用前)。Owner:** `seigo-gace/ai-core` / Issue #1。  
**Masterの確定要件:** 2026-10-13 の会話（通常最大2、引継ぎ時一時3、処理終了後は2以下、既存36GiB/6coreを維持）。  
**参照した採用設計:** [Notion 改訂F／決定記録](https://app.notion.com/p/3f8cdcf128e4819eb94af16523d8a0b7)。「既存Runtime再利用＋薄い共通資源調停＋モデル別最適化」。  
**既存実装:** [PR #3](https://github.com/seigo-gace/ai-core/pull/3) は開発中であり、この設計PRのマージ以前に改訂設計の採用証拠としない。

## 1. 開発目的と変更不可条件

既存の4種類のllama.cppモデルを**全て選択可能**に保ちつつ、固定的な4モデル常駐で消費しているAI用RAM/CPUを、実際の推論処理・Context/KV Cache・引継ぎ作業・切替起動に再利用する。利用するAI ProjectはDebugAI限定ではなく汎用。既存モデルの精度・役割対応と既存実行環境を壊さない。

- 全AI用親cgroupの承認済み上限：**MemoryHigh 34GiB / MemoryMax 36GiB / CPU 6 cores**。新たに上限を増減したり、再承認を求めたりしない。Host他サービスの領域も従来どおり保護。
- モデルは従来のGranite/Qwen3/Ministral/Coderの**4種類**。モデル重量を8B→2Bへ置換する指示ではない。
- 本番旧共通API `127.0.0.1:18080` と既存バックエンド `18081–18084` を既存利用者の無検証切断なしに維持。ユーザー向け公開Port・新Repo・新MCP・新Workflow・新Token・巨大Managerは作らない。
- **通常時の同時メモリ常駐：0～2モデル（モデル名は固定しない）**。2枠は「どのモデルか」ではなく共有する通常運用枠。
- **引継ぎ・切替時の一時的な同時常駐：最大3モデル**。新旧モデルを同時に使用できる時間を設け、その後に旧モデルを解放し**通常0～2モデルへ戻す**。3モデル常駐を通常時の新しい定常値にはしない。
- リソース管理はAI Core内で共有化。各AI Projectのビジネスロジック・役割選択・プロンプト・成果物評価・実際の引継ぎ内容をAI Coreへ取り込まない。

## 2. 資源効果のイメージと実測基準

1モデル約8GiBを概算した設計イメージ（モデル重量・cache・オーバーヘッドにより変動）：

| 状態 | 常駐モデル数 | 概算常駐モデルRAM | 36GiB枠からの概算余力 |
|---|---:|---:|---:|
| 従来 | 4 | 32GiB | 4GiB |
| 通常稼働 | 最大2 | 16GiB | 20GiB |
| 引継ぎ・切替中の一時状態 | 最大3 | 24GiB | 12GiB |
| 引継ぎ完了後 | 最大2以下 | 16GiB以下 | 20GiB以上 |

**これは予算計算であり、保証される空きメモリではない。** 解放後の実余力は親cgroup `memory.current`、各backend cgroupの匿名/file RSS、Swap、PSI、キャッシュ・利用タスクの増分で確定する。CPU quotaは予約・固定占有ではないため、モデルの停止で何コア増えたと機械的に主張せず、実CPU利用、throttling、処理速度/TTFTを観測する。

**変更前実Hostベースライン (2026-10-13):** `docker stats --no-stream` 4モデル合計 **32.129GiB** (Granite 7.755 / Qwen3 8.199 / Ministral 8.664 / Coder 7.511)。[Host証拠](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6147302507)。親 `memory.current=35,589,017,600 B`、High=34GiB、Max=36GiB、CPU=6、Swap=0。[親cgroup証拠](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6147333409)。`memory.events` のOOMは累積値であり最新発生と断定しない。

## 3. 責務の境界

| AI Core / 共通所有 | AI Project / 各製品所有 |
|---|---|
| 4モデル共有Registry、個別endpoint/互換ルート、認証境界 | 役割→モデルの選択、Thinkingモード |
| 全製品共通のLease/Admission、最大同時実行数、bounded queue/期限 | Project固有のタスク順序、プロンプト、出力予算、回答品質 |
| モデル起動・readiness・通常常駐枠・一時引継ぎ枠の計測と制御 | 引継ぐ情報の生成と受け手への指示・業務上の完了判定 |
| 引継ぎトランザクションID、段階/期限・所有権/lease管理、失敗回復 | 引継ぎ開始・成果確認・完了/中止のAPI通知 |
| 停止対象の安全選択、モデル停止、資源回収の実測、復旧 | 利用モデルの正当なlease解放、キャンセルと結果利用 |

AI Coreは「引継ぎ内容を理解する」必要はない。対象モデル・使用主体・期限・成否と既存リソース状況を管理する。

## 4. 実行状態とモデル切替の状態遷移

`resident_count` はOS/Dockerで実際に生きているモデル数。状態フラグの数では代用しない。状態は `STOPPED → STARTING → READY → ACTIVE → IDLE → STOPPING → STOPPED` と障害時 `FAILED/RECOVERING`。既存コンテナのstart/stop、ready確認を再利用する。

**通常リクエスト:** 対象がREADYならLease取得・使用。未稼働なら余裕枠に起動、2枠が満杯なら利用中でないモデルを選んで停止・**実停止/必要資源を確認**してから必要モデルを起動。同時2実行の既存global制御は維持し、起動中のモデルへ競合起動しない。停止中/移行中のモデルに到来した要求は有限キューで扱うか明確な429/503と期限を返す（既存方針との整合を確認）。

**引継ぎ:** 利用Projectが旧モデル・新モデルを指定して一時移行Leaseを取得。AI Coreは新モデルの起動前に共通予算を確認し、通常稼働が2モデルの際でも**一時3モデル目**の起動を許す。新旧モデル両方を稼働可能に維持し、呼び出し側は所定の情報を移して完了を通知。完了時に旧モデルがまだ他のProjectからLeaseされていないことを確認し停止・資源解放、`resident_count <= 2` に戻す。旧モデルが他の正当な処理で利用中なら強制停止しない。解除を待ち、他の遊休モデルを先に解放するか期限付きで復旧待機とする。

**失敗・例外:** 起動失敗、期限超過、Projectのキャンセル、プロセス切断、異常終了を対象に一時枠を予約解除する。新モデルだけが失敗したなら旧モデルの正常利用を継続できるようにする。既存のモデルが他Projectで使用中なら停止禁止。孤立した一時3常駐状態を定期的に回収・監査し、上限や資源不変条件を回復するまで新たな一時予約を拒否。ロールバック中もRAM/CPU予算を守る。

**引継ぎ開始時点ですでに3以上常駐**、既存4常駐本番、古い18080がLeaseを通らないときは、強制evictionで状態を偽装せず独立移行段階で解消する。冷起動と同時に一時4常駐を作らない。

## 5. 共有Admission / API契約（設計候補）

既存ローカル個別APIと18080互換は維持し、全てのAI Projectが**同じLease所有者**を経由する。モデルAPIの認証、allowlist、loopbackバインド、モデルID検査、全体同時数2・モデルごとの既存 `-np 1` を維持する。既存モデルAPI経由の無制限直呼びで資源保護を迂回させない。

最小の資源制御操作（概念契約、具体URL・メソッド・versionはSource整合後決定）：
- `acquire(model, project, deadline)` / `release(lease_id)`: 通常の推論使用権。Project固有のrole情報は透過的な監査属性。
- `handoff_begin(from_model, to_model, project, deadline, existing_lease?)`: 引継ぎ中の一時3枠予約・同一組合せの重複予約防止・共通資源予算確認。複数Project同時引継ぎで一時枠数を超えない。
- `handoff_complete(handoff_id)` / `handoff_abort(handoff_id)`: 完了後の旧モデル解放手順または安全復元。期限切れは明示状態へ遷移し回収する。
- `model_status` / `usage_metrics`: 4モデル状態、実同時常駐数、稼働leaseと一時3枠、親cgroupメモリ/CPU、queue/timeout、実回収値をSecret非表示で返す。

個別APIを4つ新規公開する指示ではない。既存局所Gate/認証境界を活用し、設計PR内ではSourceを変更しない。現行 `runtime/model_gate.py` と既存 `router/scripts/*` に二重管理が発生しないよう、実装前に所有者を一意にする。

## 6. 改修順序とOSS活用

1. **現行F優先1:** 4モデル一律の `-c8192/-np1/--cache-ram4096`、CPU上限、各モデルRSS、PSI、Qwen3/Ministralの待機・遅延を実測し、モデル別profile候補の効果を比較。既存チューニングの破棄は禁止。
2. **F優先2:** 既存llama-swap v257の必要時ロード・TTL・モデル共存機能が使えるかSource/実環境で検証。既存router契約 `models: {}` は現在backend lifecycle移管を禁止しているため、Source契約を無断変更しない。ここが使えない場合は既存認可済みDocker start/stop方式を最小限で利用。
3. **F優先3:** 現行PR #3の薄い共通CoordinatorとAPIを再利用し、固定2絶対上限・固定warm・失敗時rollbackを本設計に従い改修。一時3枠/引継ぎLease/期限/孤立回収/重複防止とメモリ観測を同じOwner内に導入する。各製品に別Managerを作らない。
4. 実Host影響ゼロでのSourceテストと隔離実装検証。旧18080利用者の排他と可逆移行を実証してから、本番の一時4→通常2、引継ぎ時一時3→通常2を測定。実際のAI製品2種類で同時利用を検証。

## 7. 既存PR #3との確認済み設計差分

- 現行 `runtime/model-policy.yaml`: `max_resident:2` で**一時3モデルを表現不可**。通常2・引継ぎ一時3・完了後<=2の状態契約が必要。
- 現行 `runtime/model_gate.py`: `Coordinator` に引継ぎID/所有Project/期限/一時枠/復帰回収がない。通常の単体Leaseと起動停止の一部は再利用可能。
- `warm:false` の全モデル固定化はCoder/Qwen3固定2常駐をやめる暫定措置であり、実測から選ぶ可変Policyと、アイドル/頻度ベースの最適化は未完成。
- 現行 `runtime/host_readiness.py` の `memory_comparison` は推定であり、停止後の実回収量を証明しない。
- 既存routerは4 peers/legacy18080と`globalConcurrencyLimit=2`を持ち、leaseを経由しないため、本番カットオーバー確認なくDocker停止を行ってはいけない。
- [Mock Coordinator 19/19](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6147270168) はSource構文と仮想モデル切替のみ。本番資源改善実証ではない。

## 8. 受入試験（Source・CI・Host・Runtimeを区別する）

A. 設計契約：通常0～2、引継ぎ時のみ一時最大3、戻り<=2、4モデル全て選択可、複数Project重複予約防止、使用中停止防止、異常終了/キャンセル/期限切れ回収、親資源上限不変。  
B. 同時制御：モデル起動中の既存READYモデル応答継続、無制限直呼び防止、global maxActive=2、最適なbounded queue、deadline破棄、lease漏れ防止。  
C. 実機：4常駐baseline→通常2後の親 `memory.current`・モデル匿名/file RSS・Swap・PSI差分、handoff一時3時の値、完了後<=2、CPU throttling/TTFT/tps/p50/p95/OOM delta、実際のRAM headroom改善を観測。  
D. 製品互換：DebugAI 6役割／4モデル（Thinking/JSON/Tool Loop/PromptCache）と別の登録AI Projectとの共有、既存18080経由移行・rollback、意味品質の非劣化。  
E. Source/CI PASSはRuntime PASSの代用禁止。実Hostで未測定なら `UNKNOWN / NOT_VERIFIED` と記載する。旧本番4常駐への無断停止を行わない。

## 9. 設計PR採用手順と権威

本書は**設計だけを変更するPR**として `main` に提案する。Work Routine §6 の `PROPOSED_PR` であり、レビュー前や未マージの段階で `ADOPTED_MAIN` と表現しない。Source PR #3 は設計差分を明記して継続管理する。採用時は設計PRをmainへマージし、READMEに設計正本への参照を置き、AI Core Issue #1 CurrentのDesign revision/実装CUを更新する。その後、PR #3のSource/Testsをこの採用設計へ照合して必要最小限の改修を続ける。

**変更権限:** ユーザーによる業務要求は本書の変更理由であって、GitHub/Hostの実行権限を自動拡張しない。
