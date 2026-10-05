high はありません。新規 5 件、すべて medium です。

1. **X-60｜実装｜medium｜費用カウンタを省略して起動できる**
   - 破綻シナリオ: `llm_budget` の既定値が `None` で、未注入なら `_reserve_send()` がそのまま送信を許可するため、再送・並行を含む全呼び出しが無計上になる（[referee.py](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:151)、[referee.py](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:525)）。
   - 直し方: 本番用 `RefereeDeps` では `LlmBudget` を必須化し、`None` は明示的なテスト専用構築口だけに隔離する。

2. **X-61｜実装｜medium｜`usage.requests > 1` を受理して物理上限を過少計上する**
   - 破綻シナリオ: executor は複数のモデル応答を `requests` に積算するが、client はモデル名とトークン数だけを検査し、`requests == 1` を要求しない。1 回の予約で複数要求が生じても、そのまま成功扱いになる（[executor.py](/Users/toshixa/dev/tenshokuagent/src/agents/executor.py:88)、[client.py](/Users/toshixa/dev/tenshokuagent/src/agents/client.py:107)、[referee.py](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:502)）。
   - 直し方: `requests == 1` を封筒の必須条件にし、モデル層でも 1 実行につき外向き要求が 1 回以下であることを強制する。

3. **X-62｜実装｜medium｜壊れたカウンタ文書を 0 とみなして fail-open する**
   - 破綻シナリオ: 日次文書または `stages/{nid}` が存在しても `count` / `llm_calls` が欠落・負値なら、0 または負値として加算を続けられ、上限を余分に消費できる。入場判定も同様に少なく数える（[llm_budget.py](/Users/toshixa/dev/tenshokuagent/src/web/llm_budget.py:114)、[llm_budget.py](/Users/toshixa/dev/tenshokuagent/src/web/llm_budget.py:175)、[llm_budget.py](/Users/toshixa/dev/tenshokuagent/src/web/llm_budget.py:199)）。
   - 直し方: 存在する文書では非 bool の非負整数を必須化し、欠落・不正・上限超過値は `LlmBudgetUnavailable` として送信を止める。移行は別処理にする。

4. **X-63｜前提｜medium｜承認済みの thinking・出力上限からの設定ドリフトを止めない**
   - 破綻シナリオ: thinking は列挙値なら任意の段階を受理し、`max_output_tokens` は上限検査なしでモデルへ渡る。誤設定で HIGH や過大値になっても起動でき、承認時の品質・1要求当たり費用の前提が崩れる（[llm_agents.py](/Users/toshixa/dev/tenshokuagent/src/agents/llm_agents.py:85)、[llm_agents.py](/Users/toshixa/dev/tenshokuagent/src/agents/llm_agents.py:94)）。
   - 直し方: 起動時に phase ごとの許可値と `max_output_tokens` の安全上限を検証し、費用見積もりの設定と一体で変更させる。

5. **X-64｜実装｜medium｜`stop_cost_limit` の削除競合で交渉 ID が例外文に入る**
   - 破綻シナリオ: 上限到達後、交渉削除と競合すると `control` が `NotFoundError(nid)` を生成する。HTTP 変換や例外ログの経路次第で nid が残り、X-40 の境界を破る（[referee.py](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:292)、[store.py](/Users/toshixa/dev/tenshokuagent/src/vault/store.py:973)）。
   - 直し方: NotFound のメッセージを固定文にし、識別子が必要なら文字列化されない内部属性として扱う。

なお、`by-request` の 404 文言はキーを含まず、uvicorn access log 用の専用マスクも確認できました。テストは実行していません。

