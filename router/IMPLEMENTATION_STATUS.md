# llama Router Core v1

目的は4つだけ。

1. 既存4 llama.cpp を1つのRouter APIから利用可能にする。
2. 同一Modelの不要な重複Loadを作らない。
3. global concurrency + cgroup v2 でCPU/RAM天井を作り、Contabo上の他Projectを守る。
4. Health/API key/Metrics/rollbackだけを最低限保持する。

含めない: Debug AI/経営AIのRole・Workflow、TGserver/P004、Vector、Telegram Receipt、追加監視Stack。

実Contaboで未実行のGateはPENDINGのまま。
