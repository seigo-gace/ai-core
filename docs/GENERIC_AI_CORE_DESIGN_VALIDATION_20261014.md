# AI Core 汎用化・責務検証 — 設計専用Change Unit

**状態:** `PROPOSED_PR` — 未採用。既存採用設計 `main@4bdb88714fe2800bada7b268a922ab61f7ec358a` の補足検討。
**Owner:** `seigo-gace/ai-core` / Issue #1。
**開発目的:** 既存AI Coreの共通推論・実行基盤を改善・汎用化し、相互に独立したAI製品が共通API・モデルLifecycle・資源調停・隔離・復旧を再利用できるようにする。
**今回の作業目的:** 既存の実装・検証済み機能と未確定事項を照合し、隔離検証を経て、最終設計の採否を正本へ反映できる状態を作る。

## 固定責務

| AI Coreが所有する | AI製品が所有する |
| --- | --- |
| 推論API、認証境界、モデルRegistry、Lifecycle、Admission/Lease、物理資源調停、隔離、異常復旧、共通観測 | 目的・役割・モデルの選択、Prompt、Thinking、Tool/DB、タスク順序、引継ぐ意味内容、成果・品質判断 |

DebugAIの4モデル・6役割は既存互換と代表検証条件であり、AI Coreの汎用仕様の固定上限ではない。現Hostの通常0～2／一時最大3とCPU6・memory.high34GiB・memory.max36GiBは導入先固有のPolicyと区別する。

## 既存資産の再利用と証拠

| 機能 | 再利用する既存資産 | 判定 |
| --- | --- | --- |
| 推論・モデル | llama.cppの個別Backend、GGUF、既存Docker/Compose | 旧Host実績あり。新運用E2Eは未証明 |
| 互換Routerと認証 | llama-swap v257、18080、model ID、既存Key/peers | 旧経路は既存。Gate側のconfig生成テストと実際の疎通は別 |
| 共有制御 | PR #3のCoordinator、単一Lease、model gate、Docker既存容器Driver | 旧Sourceで45/45のCoordinator/HTTP等模擬テストPASS。現在の全機能や実Hostに一般化しない |
| 動的常駐・引継ぎ | 既存のcold/evict、handoff reservation、終了・失敗時回収 | Source模擬テストあり。実Host自動2→3→2未実証 |
| 物理資源 | systemd cgroup、memory.current/PSI、host_readiness | 旧Host4→2停止時に17,044,062,208B減少を観測。新構成の性能保証ではない |
| OSS部品候補 | v257のTTL/eviction費、llama.cppのSleep/cache/slot | upstreamで存在。現稼働版・製品隔離・CPU効率を検証して採用 |
| 製品間公平性と復旧 | 既存SemaphoreとHost記録・リカバリー処理 | 不足する汎用契約を比較し、必要なら改善／OSS導入／新規機能 |

証拠：[Source 45/45](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6158958379)、[Gate peer生成](https://github.com/G-ACE-inc/server-core/issues/10#issuecomment-6158985118)、[Host手動RAM減少](https://github.com/seigo-gace/ai-core/issues/1#issuecomment-6152014424)。

## この設計CUで実施すること

1. 既存実装と採用設計の機能・責務を1対1で照合する。成功済み部分を再作成しない。
2. 異なるモデル登録と複数独立製品に対し、共通API・Lease・安全隔離・公平待機・障害復旧の設計を検証する。
3. 必要な場合のみ、既存の隔離ブランチまたは登録済み隔離コンテナで小規模な**設計検証**を実施する。試作変更を実装PRへ移植しない。
4. 未解決の候補は、維持／改良／OSS部品導入／必要な新設／不採用を比較し、その根拠を本PRへ記録する。
5. 機能別のSource模擬PASS、隔離PASS、実Host、二製品E2Eを別々に記録し、未知をPASSにしない。

## 受入条件と制限

- 二つ以上の独立した製品プロファイルで、製品側の責務を侵さず共通の基盤契約を利用できる設計であること。
- モデル名・個数が現行4モデルにハードコードされた共通制御と、現Host Policy固有の制限を明確に分離できること。
- LifecycleとLeaseのOwnerは一意。経路の迂回・他製品の利用中停止・キャッシュの文脈混線を許さないこと。
- 障害・取消・期限・再起動・冷起動とREADY応答・一時3モデルの後始末を明示すること。
- 旧互換APIと有効な既存技術を維持し、OSS部品選択と不足機能の新設を比較理由付きで決めること。
- **実装PR #3、実行Source、Docker/Host、本番、Registry、Workflow、Credential、既存採用設計mainはこのPRの変更範囲外。**
- **PRはDraftで維持し、未確定の検証が残る間はマージしない。** 実装許可は本PRやテストPASSから自動的に発生しない。

## 未確認・後続の検証

- 最新feature PR #3と旧PASS対象SHAの差分、および検証の再現性。
- 旧18080→Gate→Backendの実トラフィックが単一Leaseを迂回しないこと。
- 複数独立AI製品での実E2E、品質・遅延・公平性・再起動。
- 現Hostのllama.cpp機能、v257動的機能の実運用適合、モデル別Profile。
- 登録済みdev.jobのHost結果通知不達（[Actions](https://github.com/G-ACE-inc/server-core/actions/runs/38517164519)）は検証経路のUNKNOWNであり、Core製品の不合格判定とはしない。

採否が固まるまで本PRを`PROPOSED_PR`として維持する。議論と証拠はIssue #1と既存Notion議事録に継続記録する。

## 会話・設計トレーサビリティ監査

既存main設計§1～14と本PRを照合。主目的＝既存AI Coreの改善・汎用化（§1/14）、Core対製品責務（§3）、既存／OSS／新規の選別（§6/11/14）、現Host固有の0～2・一時3と資源上限（§1～5）、旧API/Lease（§5/10）、冷起動・期限・Handoff・復旧（§10～14）、v257/llama.cppの部分採用（§11/14）、Sourceと実機の証拠分離（§8/12/14）、設計検証と実装の禁止境界（§12/14）には記述がある。

ただし以下は**詳細設計・実証として未完成**：①複数製品共通APIのproduct識別とversion/エラー契約、②可変モデルRegistryとHost Policyの独立性、③load枠・active枠・fair queueの不変条件、④再起動時Lease照合と製品・会話別Cache隔離、⑤現HostのOSS対応実測、⑥独立した二製品での受入試験。DebugAI固有の4モデル/6役割は汎用化の上限ではない。45/45過去Source PASSを最新全体や本番PASSと扱わない。未確定6点をこのDraft内で解消し、完成前にマージ・実装しない。

## 汎用API・状態・Ownerの詳細設計候補（未採用）

**登録:** モデルは `model_id`、backend endpoint、runtime capabilities、model-specific resource profile、現物identityと互換contract revisionで登録する。既存4モデル名は初期データであり、汎用Coreコードに固定しない。特定Hostで適用される `max_resident`、`max_active`、`temporary_handoff_capacity`、memory/cpu上限は独立したHost policyとして検証する。

**要求:** 認証済みrequestは製品境界識別子、`request_id`、`model_id`、deadline、stream指定を受付。PromptやThinking等の推論payloadを透過的に転送し、その意味をCoreが判断しない。製品識別子はクライアントの自己申告だけで信頼せず、既存認証・委任された実利用者と突合する方式を検討する。新Tokenを作ることはこのCU範囲外。

**同時実行:** モデル起動のreservationと推論のactive leaseは別の資源。実起動中は物理CPU/RAMを消費するので二つを完全独立の無制限枠にはしない。既READYへの要求を不用意に429にしない一方、Coldの飢餓を招かない有限Queue・期限・キャンセルを比較する。既存のglobal semaphore、各modelの`np=1`、現Host上限を維持する。

**機械的引継ぎ:** `owner`、旧・新model、ticket、startup deadline、READY後transfer deadline、物理resident予約、active leaseを保持する。開始・READY・完了・中止・期限切れ・再起動を分ける。業務上の引継ぎ内容や成功判定は製品が担う。既存transition guardと物理停止確認を再利用する。

**再起動:** 実Docker running/readinessとCore記録が不一致なら安全側で停止を保留し、新規Admission・Leaseと所有者を検証する。既存利用者の未確認通信を遮断したとの推測でモデルを退避させない。Slot/KV/cacheは製品・会話境界を保ち、再利用による他製品の文脈混入を禁止する。

**互換:** 旧18080、モデルID、Bearer、Streamingとエラーの外部契約を無検証で変更しない。新しいスキーマは提案候補として扱い、既存の実エンドポイント／現Source契約との差分を確認してから採否する。

## 設計検証シナリオと証拠判定（未実施）

| シナリオ | 隔離設計検証で確認する結果 |
| --- | --- |
| 製品Aと製品Bが異なるモデル・Roleを使用 | 同一Core契約の下でそれぞれが独立した業務判断を維持し、他方のLeaseや文脈を侵害しない |
| モデル名・数が既存4種とは異なる | RegistryとHost Policyを変更しても共通Coreの実行・資源管理契約が成立する |
| Cold起動と別のREADY要求が競合 | Ready推論が長時間ロックに巻き込まれず、load/activeとRAM/CPU予算が整合する |
| 複数製品のQueue競合と期限切れ | 公平性、bounded wait、キャンセル、Cold飢餓防止、明示的な過負荷応答 |
| 通常1→2、2→一時3→2、target既READY | 既存lease保護、物理resident計測、失敗時再照合を満たす |
| Streaming上限超・client切断 | 完走と途中切断を区別し、正当Leaseを残さない |
| Core再起動、Docker側停止遅延、cache再利用 | 所有権不明で強制停止せず、異なる製品・会話への文脈混入を防ぐ |
| v257とllama.cpp標準機能の部品導入候補 | 実Host BuildとOSS版の機能・品質・CPU/RAM・維持費を同条件で比較 |

**判定区分:** `DESIGN_DOCUMENTED`（仕様記載）、`SOURCE_VERIFIED`（exact SHA）、`ISOLATED_VERIFIED`（隔離実行）、`HOST_VERIFIED`（実機）、`MULTI_PRODUCT_E2E_VERIFIED`（複数製品）。一段階の合格は上位の合格を意味しない。現時点の新シナリオは設計記載のみ。Master指示が設計と隔離検証である間はPR #3 Source、本番Docker、mainを変更しない。

**採用判定:** 既存検証済み部品の再利用可否、独立製品向け共通契約、既存技術／OSS／新設の理由、残るUNKNOWNと反証条件がすべて記録された後にだけDraft解除と採用判断を行う。残項目を隠して「抜け漏れなし」と言わない。
