# AI Core 本番実行テスト計画（提案・未実行）
Status: PROPOSED_PR / planning-only / 2026-10-19
Owner: seigo-gace/ai-core Issue #1
Baseline: main 8574e9195086dd3cfa2b5b8f60766e924026413b
Authority: docs/DYNAMIC_RESIDENCY_HANDOFF_DESIGN.md §§1,3,4,8,14.4,15 and Issue #1 CURRENT (latest section first).

## 目的・範囲
現在の本番AI Coreの完成状態を、新しい観測証拠で、過去のPASS判定を今回のPASSとして流用せずに評価する。既存コード・試験シナリオ・計測器・過去のトラブル知見は再利用する。旧18080 Router復旧、DebugAI自体の6役割修正、新規Workflow/MCP/Token/監視基盤、他製品Source改変は対象外。設計の古い節と§15が競合するとき、停止済み旧Routerを受入前提にしない。

## 原則
1. 各要求は Case ID → 仕様根拠 → 実行条件 → 観測値 → 判定 → 証拠IDで追跡する。未実行は NOT_RUN、証拠不足は UNKNOWN、実行不可は BLOCKED とする。
2. 既存の実行結果を再PASSとして数えない。ただし試験手順・期待値の妥当性と故障履歴を再利用する。重複テストは共通の一つの実行へ束ねる。
3. 実Hostの起動停止・負荷付与は既存の認証済み共通Terminalと現行権限内に限定する。本番トラフィックを守り、モデル使用中は強制停止しない。隔離で証明できる異常系を本番で破壊的に再現しない。
4. PASSはSource、CI、Host、Runtime、製品E2Eを区別する。過去の5件の性能サンプルでは安定したp95を確定できない。

## 必須ケース（初期実行は必要最小限）
| ID | 根拠 | 一回の実行で観測すること | PASS基準 | 既存資産 |
|---|---|---|---|---|
| C01 | §15 | main/実Host/Gate Source同一性、稼働状態、Router廃止 | 意図されたSourceと4 Gateの健康状態が一致、旧18080停止 | Git SHA、Docker health、runtime/host_readiness.py |
| C02 | §§1,14.4 | 認証済み4モデル別Gate・モデルID、非認証拒否、非stream/streamの完了 | 全4モデルへ適正アクセス可能、不正要求拒否、本文と終端が仕様一致 | tests/test_model_gate.py、既存Gate |
| C03 | §§4,8,15 | 通常実常駐0–2→引継ぎ中のみ3以下→終了後2以下、Lease・資源回収 | 実コンテナ数とCoordinator状態一致、使用中を停止しない、解放が計測できる | 現行Coordinator、Docker/cgroup |
| C04 | §§1,8,15 | cgroup CPU、MemoryHigh/Max、swap、PSI、OOM差分 | CPU quota6、High34GiB、Max36GiBの設定維持、意図しないOOM増分・重大圧力なし | cgroup v2、host_readiness.py |
| C05 | §§8,14.4 | 別client/製品の競合、Queue上限、Deadline、Cancel、公平性とLease解放 | 誤った二重利用、飢餓、期限後処理、Lease残留なし | Gate内メトリクスと既存テスト |
| C06 | §§8,14.4 | stream中断、上流異常、上限、再起動・Abort・Rollback | 未完了を成功と記録しない。復旧後不正な3常駐・利用中停止なし | 既存Source異常系試験、可逆Host受入 |
| C07 | §§14.4,15 | AI Coreを利用する独立した2製品で共通契約と隔離を観測 | 両製品のCore推論成功と相互影響なし。ただし各製品固有のE2E完走は製品owner判定 | 既存製品Adapterと実仕事トレース |
| C08 | §14.4 | モデル登録名・数の設定依存性 | 既存4名に固定されないCoordinator契約を隔離で確認 | policy loader、mock driver |
| C09 | §§2,8,14.4 | 同条件の応答品質、成功率、E2E、TTFT、TPS、Cold start、RAM/PSI | 採用済み目標値に照らして判定。値未定は基準化しPASSとしない | /internal/metrics/status、既存計測保存 |

C02の推論データをC09に使用し、C03の操作とC04の資源記録を同時実施。C05・C07の自然な製品負荷があればC09と共用。異常系は隔離の既存テストを基本にし、実Host実施が不可欠な未証明の境界だけ一度実行する。

## 目標値の決定と判定
固定契約: resident通常<=2・引継ぎ中<=3・完了後<=2、active<=2、CPU6 core、memory.high=34GiB、memory.max=36GiB、旧18080停止、4モデル選択可。memory.highはsoft閾値であり、瞬間超過のみでFAILとしない。OOMは累積値ではなく今回前後の差分を見る。

性能SLOが未設定の場合は今回の測定を BASELINE_ONLY として保存し、Masterの承認済み製品品質基準と採用済み要求に照らして目標を別途確定する。数値を恣意的に創作しない。対象モデル、温冷状態、入力・出力長、同時数、製品、モデル設定、測定窓を揃える。p50/p95を出す前にn・失敗件数・測定窓を必ず表示し、少数nのp95は運用目標の証明にしない。必要標本数は許容誤差・要求する信頼度と実際のばらつきから決め、固定回数を全ケースへ機械的に適用しない。

状態区分: PASS / FAIL / BLOCKED / NOT_RUN / UNKNOWN / BASELINE_ONLY。FAIL後は原因・修正によって影響したCaseのみ再実行する。テスト実行回数自体を成果にしない。

## 実行順序・停止条件
(1) C01と実施安全性のread-only確認 → (2) C02、C08の必要な隔離確認 → (3) C03+C04 → (4) C05+C07の実仕事共有 → (5) C06で未証明の復旧境界 → (6) C09の既存データから判定、証拠保存。実際の利用者処理が稼働中、権限不一致、資源余力不足、Rollback不可の場合、影響する試験だけBLOCKEDとして他の安全な試験を進める。

観測記録の最小単位: Case ID / UTC時刻 / Commit+Runtime Source ID / Model+client label / シナリオID / 比較条件 / 実測値と単位 / n / 判定 / 失敗種別 / Run・Request・Host evidence URL / 次の影響Case。Secret、Prompt原文、認証データは永続記録へ含めない。

## 再利用と変更時の回帰
- tests/test_model_gate.py: Unit/API/Mockで再現できる境界だけ。
- runtime/model_gate.py、runtime/model-policy.yaml、compose.runtime.yml: 現実のGate・Policy契約。
- runtime/host_readiness.py: 読取専用Host資源と状態。
- scripts/host_gate_refresh.sh: Source変更があり本当に反映する際の可逆手順。計画のみの段階では使用しない。
- /internal/metrics/status: 実推論・遅延・TTFT・TPS・計測の永続化を確認。

文書のみの変更は実モデルを起動しない。Gate HTTP差分はC02/C06、Coordinator差分はC03/C05/C06、資源設定差分はC03/C04、製品Adapter差分はC07、性能差分は同条件C09に限定して再実施。重大な設計変更時は対応表を見直す。

## 現在の限界と次のゲート
これは計画文書であり、本番ケースは未実行。全テストファイルと全現場シナリオの完全な棚卸しはまだ未確認であり、実行前にCase-IDとの対応と冗長・不足を一度見直す。今回の新たなPASS/性能基準/長期安定性は主張しない。Issue #1に紐づくこのPRを審査し、重複や実施手順を修正してからmainに統合する。