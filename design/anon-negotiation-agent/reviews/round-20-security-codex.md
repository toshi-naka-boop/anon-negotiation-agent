> critic: codex/gpt-6.1-sol reasoning effort xhigh（CLI 0.160.0。891 秒。ログは round-20-security-codex.log）— 反証 20 巡目（安全性）。指摘の ID は台帳で X-85〜X-87 を振る

静的レビューで3件です。ネットワーク接続・試験実行・禁止箇所の読み取りは行っていません。

1. **実装／medium — `begin` の本文解析が認証・レート制限より先に走る**

   **破綻シナリオ:** Cookie なしでも `X-Requested-With` を付けて大きな JSON を送ると、FastAPI が本文全体を読み、解析してから認証・追加した制限を評価する。不正 JSON は制限に到達せず終了するため、繰り返すと公開 web のメモリと CPU を枠外で消費できる。LLM 入力の既存の 32 KB 制限とは別の経路。
   
   **該当箇所:** [src/web/interview/api.py:125](/Users/toshixa/dev/tenshokuagent/src/web/interview/api.py:125)、[src/web/session_middleware.py:75](/Users/toshixa/dev/tenshokuagent/src/web/session_middleware.py:75)。
   
   **直し方の方向:** 自動の本文宣言を外し、認証・制限後にサイズを数えながら読む。公開 POST 全体にも、JSON 解析前の本文上限を設ける。

2. **実装／medium — agents の IAM 照合が継承権限と custom role を見落とす**

   **破綻シナリオ:** 別の SA にプロジェクト階層で `roles/run.invoker`、または `run.routes.invoke` を含む custom role が付いていても、サービス直付けの invoker が web SA だけなら照合は OK になる。その主体は agents を直接呼び、web の入口制限・日次 LLM 計上を経ずに推論を実行できる。現在その付与が存在するという指摘ではなく、誤設定を合格にする照合の抜け。
   
   **該当箇所:** [scripts/deploy_check.sh:520](/Users/toshixa/dev/tenshokuagent/scripts/deploy_check.sh:520)、[scripts/deploy_check.sh:1320](/Users/toshixa/dev/tenshokuagent/scripts/deploy_check.sh:1320)、[src/agents/executor.py:134](/Users/toshixa/dev/tenshokuagent/src/agents/executor.py:134)。
   
   **直し方の方向:** サービスと上位階層を対象に、`run.routes.invoke` の実効権限を custom role 込みで列挙・照合する。許容する管理主体も明示し、未完了の解析は合格にしない。

3. **設計／medium — 面談500枠を、制限内の作成と GET だけで占有できる**

   **破綻シナリオ:** 匿名セッションを順次作り、`begin` の10回／10分を守りながら500件まで蓄積する。各状態を1時間未満の間隔で GET すると寿命が更新され、定期掃除でも消えない。維持には約500 GET／時で足り、通常利用者の新規面談は `503 too_many_interviews` になる。これは台帳 **I-34 の未解決事項**であり、解決済みの「入口に枠がない」という指摘ではない。
   
   **該当箇所:** [src/web/interview/api.py:128](/Users/toshixa/dev/tenshokuagent/src/web/interview/api.py:128)、[src/web/interview/state.py:92](/Users/toshixa/dev/tenshokuagent/src/web/interview/state.py:92)、[config/params.toml:278](/Users/toshixa/dev/tenshokuagent/config/params.toml:278)。
   
   **直し方の方向:** IP ごとの同時保有数を開始時に制限する。読み取りだけでは無期限に延命できない寿命・解放条件も定める。

