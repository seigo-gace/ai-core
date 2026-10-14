# 旧Router再利用・段階移行契約（既存AI Core PR #3）

Authority: [採用設計](../docs/DYNAMIC_RESIDENCY_HANDOFF_DESIGN.md) / AI Core Issue #1。これは現行設計の実装補助であり新規独立Router提案ではない。

## 残すもの（旧製品の有効資産）
- **llama-swap v257本体・pin SHA**：`router/Dockerfile.llama-swap` と `router/evidence/llama-swap-v257-contract.json` による固定バイナリ・検証済フラグ。別のRouterは増設しない。
- **互換APIと認証**：loopback `127.0.0.1:18080`、既存 `LLAMA_SWAP_API_KEY`、`/health`、OpenAI互換モデル照会/チャット、4つのモデルID、既存proxyのレスポンス契約。
- **同時処理数**：`router/scripts/generate_config.py` が生成する `globalConcurrencyLimit:2`。CPU親上限6coreと混同せず保持。
- **4 peer / 実backend identity**：`granite/qwen3/ministral/coder` と `18081–18084`。既存Compose container・モデル重量・HostConfig・cgroupを再利用。
- **資源と安全保護**：`router/scripts/resource_ceiling_gate.py`、`runtime/model_gate.py` の既存Lease/Admission、`router/scripts/runtime_contract_gate.py`、`ai-core.slice` MemoryHigh34GiB/Max36GiB/CPU6core、rollback用既存Compose/config。
- **事前調査・状態検証**：`router/scripts/preflight.py`、`runtime/host_readiness.py`、既存Router経由の機能テスト。

## 取り替える責務（旧Routerの「全削除」ではない）
既存llama-swapの **peersモード** は `models: {}` を要求する。そのためproxyがバックエンドを直接呼び出すだけでは、モデルのLease/stop禁止や動的2→3→2状態を把握できない。**既存Routerから新しい共有Admissionを迂回させないこと**が唯一の切替成立条件。

通常のリクエストは 18080互換ルーター → 所有が一意の共通Leaseを通す個別Gate → 既存backend とする。引継ぎbegin/complete/abortは既存のLoopback認証済みGate APIを使い、業務引継ぎ内容は各AI Projectが管理する。ルーターにDocker socketを渡さない。新しい独立Router/Token/Workflow/Managerを作らない。

**重要**：現行 `generate_config.py` は旧設定との互換のため通常は `18081–18084` を生成するが、`--peer-target gate --exclusive-traffic` 指定時には **18181–18184 のGateを宛先にし、既存 `LLAMA_SWAP_API_KEY` を `peers.*.apiKey` マクロで渡すSource実装がある**。Source生成と認証値構造のテストはPASS。ただしこれだけでは旧18080トラフィックの排他、本番Proxy→Gateでの認証・stream・rollbackは確認されず、**実Hostの切替・実用化は未完了**。一方、現行 `model_gate.py` は個別Gateポート `18181–18184` と1モデル固有IDを前提にする。既存Routerの `peer` 宛先をGateに変える実装は、4 Gate全稼働、ルーターモデルID保持、ループ・認証転送、404/401/streaming互換、異常時の旧設定復元を確認してから行う。変更前には旧18080で通信する全クライアントと認証方法を調査する。

## 切替手順・停止条件

1. 既存Hostの4コンテナとRouter config/systemd/Dockerの所有者、既存18080通信元とAPI契約を採取。Sourceブランチと本番ディレクトリを区別する。
2. 既存worktree `.worktrees/source-import-20261013` に最新Sourceを取得して隔離検証する。Source取得はRuntime反映ではない。
3. Gate 4個を同じ共有Coordinatorで起動し、認証・同時処理・model ID互換を実測。現在18181だけListenという以前の実測は、全Gate readiness の証明ではない。
4. 旧RouterのAPIとKey/IDを保持しながら **18080を単一の新入口へ切替**。短時間の接続確認だけを排他証明にせず、旧直通経路を停止/隔離してLease迂回をなくす。切替失敗時は旧config・旧routerへ戻す。
5. 共有Lease入口を確認した後に、利用中でない2モデルを停止して通常0–2へ。停止前後で親 `memory.current`、backend RSS、Swap、CPU/TTFTを実測。
6. 一時引継ぎ2→3→2と期限・失敗時の回収を検証。旧モデルに活動Leaseがあれば強制停止せず、適切に再試行。

## 実測と未完

- 旧4モデルDocker stats合計32.129GiB、親memory.current約33.15GiBは旧状態のベースラインであり、実回収量ではない。
- 実HostでGit fetchとCoordinator 31件の実SourceテストはPASS。ただし **本番Source reflect、Router切替、Docker停止、RAM回収は未完**。
- 保護Host Registryのai-core登録は現在`host.command`、汎用`service.deploy`/source reflect権限を自動的に含まない。権限不足は別システム新設でなく既存#10の資格管理と照合する。
