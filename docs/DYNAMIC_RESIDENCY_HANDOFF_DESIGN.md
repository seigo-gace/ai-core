# AI Core 可変常駐・引継ぎ・共有資源再利用 — 設計正本候補

**Authority status:** 設計PR #4 のmerge前は `PROPOSED_PR`。GitHub `main` にmergeされた時点から当該 `main` 版を `ADOPTED_MAIN` とする（履歴はPR #4およびmerge commitで追跡）。**Owner:** `seigo-gace/ai-core` / Issue #1。  
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

## 7. 既存PR #3との設計差分（初期時点の履歴。最新実装との差分は第10節）

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

本書は**設計専用PR #4**として `main` に提案する。Work Routine §6 の `PROPOSED_PR` はPR #4 merge前の状態を指す。`ADOPTED_MAIN` はmerge完了後の `main` 版のみに適用する。Source PR #3 は設計差分を明記して継続管理する。採用時は設計PRをmainへマージし、READMEに設計正本への参照を置き、AI Core Issue #1 CurrentのDesign revision/実装CUを更新する。その後、PR #3のSource/Testsをこの採用設計へ照合して必要最小限の改修を続ける。

**変更権限:** ユーザーによる業務要求は本書の変更理由であって、GitHub/Hostの実行権限を自動拡張しない。


## 10. 2026-10-14 第二次設計検討 — Runtime安全契約（実装照合後の設計改訂案）

**状態:** 本節は設計改訂PRの審査対象。mainへのmerge前は未採用。第7節の「handoff未実装」等は初期時点の履歴で、現在のPR #3実装の機能不存在を意味しない。実装には `handoff_begin/finish/expire` とSSE forwardingが存在するが、本番E2Eは未検証。過去Sourceテストは同一SHAの証拠だけに紐づける。

### 10.1 一意の資源所有と移行安全性（P0）

- 各backendの実際の推論呼出元（旧18080、個別Gate、直接18081–84、他製品）を全列挙し、全て単一CoordinatorのLeaseを取得することをHostで確認する。認証済みRouterだけでも、backendへの直行経路が残れば排他性は未成立。移行前のStage Aでは推論・停止を実施しない。
- active=0はCoordinatorが把握したlease数でしかなく、未登録のlegacy利用がある限り停止可否の証明にはならない。単一Ownerと交通切替を実測するまでevictionを禁止する。
- Gate再起動後はDockerのrunning/healthと自分のin-memory状態の不一致を照合し、未追跡のlease、引継ぎticket、外部使用の可能性が残るときは停止操作をfail-closedとする。lockfileは複数Gateプロセス抑止には有効でも、旧Routerからの直行推論排他の証明ではない。

### 10.2 引継ぎの一般契約（P0）

- `from` と `to` は異なるモデルであり、前後どちらが既にREADYであっても、通常residentが0/1/2のどの状態でも引継ぎ要求を表現できる。新モデル既存residentなら新しい常駐枠を消費しない。必要なときだけ一時枠を使い、常に物理的resident数を正本とする。
- transitionは予約→資源admission→新側ready→両者の正当利用→完了/中止/期限切れ→安全な回収→通常<=2を一つのtransactionとして扱う。ticketはownerと結びつけて比較し、別製品のleaseを強制解除しない。
- 実装の `handoff_begin` は現在 `len(loaded)==max_loaded` と `target not in loaded` に限定されるため、この一般契約への差分がある。回収不可のときは予約を保持して再試行し、期限だけを理由に使用中モデルを停止しない。再起動後も未清算の3常駐を見逃さず、実使用が不明なら安全停止ではなく保留扱い。

### 10.3 資源Admissionと公正な実行（P0）

- 親のMemoryHigh34GiB、MemoryMax36GiB、CPU6coreとモデル別制限は不変。起動判断は物理running数、親memory.current/anon/file、memory.events差分、PSI、CPU throttlingと必要予約量の測定を使い、hard ceilingと高圧力下の遅延・拒否を区別する。メモリ計算式のみを実RAM回収実績とは呼ばない。
- 要求はdeadline/cancelを保持する有限queueを通し、ready-modelへの正常要求をslow start/stopに巻き込まない。製品間に偏った連続選択や飢餓を防ぐが、業務優先度・役割選択は製品側の責務に留める。現行nonblocking semaphoreの429連発はこの設計の完成形ではない。
- 固定idle 120秒や全モデル同一 `threads3/context8192/cache4096` を最適値としない。cold start頻度・再利用率・p95/TTFT/tokens/sec・処理完了までの総時間・memory/PSIを、DebugAIの複数役割と第二製品の同時負荷で比較して動的policyの閾値を選ぶ。根拠が出るまで既存profileを無断変更しない。

### 10.4 HTTP/streamと製品分離（P1）

- 非stream/stream双方でstatus・error・response schema・tool_calls・JSON/Thinking/finish_reasonを既存consumer契約と照合。SSEは正常終了マーカー確認、upstream非200、途中切断、client disconnect、cancel、deadline、backpressure、上限超過について、200を返したまま正常終了を装わず障害を検出できる仕様とする。
- 現行SSE転送は総バイト数が `MAX_RESPONSE_BYTES` を超えるとループをbreakし得る。完結性保証の修正とテストが必要。レスポンス取得中のLease保持が存在することと、完結性が証明済みであることは別。
- Project/tenantを超えるKV・prompt cache共有は誤った文脈混入がないことを前提とし、同一モデルの物理共有と秘密文脈の共有を区別する。AI Coreは技術的隔離と認証、製品はPrompt/Thinking/Toolと意味的引継ぎを所有する。

### 10.5 証拠・受入順序（変更不可）

1. Source最新SHA/既存Router依存/テスト/設計の同時差分確認。設計採用を先行し、実装は既存Draft PR #3内に統合する。
2. 同一Source SHAでunit/異常系・Host構成の検証。Source/CIのPASSは本番の証明と区別する。
3. Stage A読取専用Gate/legacy利用者の実測と単一Lease所有を確証。直backendを含む排他不明なら停止・切替を開始しない。
4. 安全なStage Bで0～2通常運用、必要時のみ一時3→2以下を実Dockerで検証。起動・中断・期限・再起動・rollback・実RAM reclaimの観測値を保存する。
5. DebugAIの4モデル6役割と独立した第二製品を同時利用し、API品質・公正性・完了時間・製品間隔離を受入。旧方式との性能比較は同じ負荷と測定方法で行う。

**明示的未検証:** 今回はHostモデル起動停止、本番traffic exclusivity、RAM reclaim、SSE本番完結、複数製品E2Eを実行していない。これらをPASSへ昇格させない。


## 11. 統合設計比較（2026-10-14、設計審査段階）

評価の目的は、4モデルとDebugAI6役割を維持しつつ、品質合格した開発作業の完了時間・成功率を改善すること。既存API・認証・cgroup・Docker・Coordinatorを最初に棚卸しし、既存改善、OSS部分導入、新規機能、廃止を責務ごとに選ぶ。既存だけに限定せず、白紙の作り直しもしない。

- 維持：既存llama.cpp四モデルと個別API、旧18080互換、認証、cgroup資源境界、DebugAIの業務役割とThinking・Tool品質契約。
- 改善：Coordinatorのロード予約と推論枠の区別、Queue/Deadline、引継ぎ状態、異常終了後の物理コンテナ再照合、SSE正常終了、初回未利用residentのidle期限、モデル別資源プロファイル。
- OSS部品として比較：llama-swap v257のcmd/cmdStop/TTL・matrix evict cost・FIFO priority、llama.cppのSleep・KV/cache・slot保存/復元。稼働バージョン、Host権限、品質、CPU・RAMへの適合を見て採用する。現在のRouterはmodels空でLifecycleを所有しないため、無断移管しない。
- 不足機能の新規構築候補：複数製品で公平な有限Queue、予約の調停、再起動回復、anti-thrashing。既存/OSSに同機能がある場合の重複実装は禁止。

Host実測では4→2停止時に親メモリ使用量が35,564,326,912Bから18,520,264,704Bへ減少した。ただし製品E2Eは未実証。現行Coordinatorは冷起動中に同時推論枠を使用し、DebugAI adapterは呼出しを直列化する。単一モデルのRAM削減だけでは製品全体の処理速度を保証しない。

比較は旧4常駐baseline、既存Coordinator改善、既存OSSの部分再利用、OSSへのLifecycle移管を、同一モデル・品質・負荷条件で評価する。主指標は実仕事完了p50/p95・成功率、cold load、cache、RAM/PSI・CPU、切替失敗、保守費用。

検証は既存server-core共通ターミナルから登録済み隔離コンテナ/開発用dev.jobを優先し、実行権限・稼働契約を確認して進める。隔離Source PASSを本番Runtime PASSと混同しない。0/1/2→3→2、期限・Cancel・再起動、READYモデルへの連座、SSE、二製品の公平性を検証する。現行handoffは起動前にTTLを開始するため、最大180秒の起動待ち後に引継ぎ猶予がなくなり得るが、transition中の停止は既存Guardが拒否する。起動期限と引継ぎ期限の分離を設計候補にする。

現時点は方式の比較設計であり、PR #3実装とHostには変更しない。検証値のない性能改善率は確定しない。


## 12. 隔離検証と運用ゲート（2026-10-14追補）

Master指示：検証が必要なら既存サーバー隔離コンテナを使い、未検証を理由に設計作業を停止しない。server-coreの共通Actions→Host経路に登録された `dev.job` を優先する。ただし2026-10-14のRegistry上でai-core隔離checkoutは `import/server-baseline-20261013`（許容 `main` も含む）に限定され、CPU1、RAM512MiB、ネットワークなし、要求timeout最大15秒。これは**小規模状態遷移・Source契約**のテスト用であり、PR #3現featureの厳密SHA試験や8GB実モデル冷起動の実測を無条件に許可するものではない。リビジョン・checkout・実行効果の実入場判定を通す。

| 証拠面 | 実施対象 | 合格条件 |
|---|---|---|
| Source/隔離 | 共有Lease、0/1/2→一時3→2、他製品lease、障害・abort・TTLと状態回収 | 同一実行リビジョンで全ケース成功、予期しない物理停止なし |
| Source/隔離 | cold起動中のREADY利用、推論実行枠とloader予約、公平queue・deadline | 無関係READY処理を誤ったglobal枠占有で拒否しないこと、資源限界では理由付き拒否 |
| Source/隔離 | SSE終端、cancel、受信上限、認証・Model ID・秘密文脈隔離 | 成功を偽装する途中切断なし、すべての失敗でLease回収 |
| 実Host専用 | Docker実start/stop、2→3→2、memory.current、PSI・CPU・Cache、旧18080全利用者 | 実際の回収と物理上限、無Lease迂回なし。隔離結果で代用不可 |
| 製品E2E | DebugAI6役割・別製品、品質/Thinking/Tool/JSON、並列調査と直列後段 | 既存成果品質を維持し、品質合格タスクの完了時間・成功率を比較 |

採否は同一負荷での品質合格率、E2E p50/p95、cold load/TTFT、RAM/CPU・PSI、二製品公平性、維持工数を比較する。受入試験が走らない場合は証拠をNOT_VERIFIEDとし、独立して進められる設計・検証を止めない。設計の詳細選定を終えた時点で本書に方式・根拠・棄却理由を記録し、既存PR #3にだけ実装差分を反映する。設計PRとSource PR、本番反映の状態を混同しない。
