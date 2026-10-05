> critic: codex/gpt-5.6-sol reasoning effort xhigh（672 秒。ログは round-10-security-codex.log）— 反証 2 巡目（安全性）。新しい指摘の ID は台帳で X-42・X-43 を振る

静的反証の結論は、X-37・X-38 は閉鎖、X-39・X-40・X-41 は残存です。新規欠陥は medium 1件、low 1件でした。テストは実行せず、`~/.config`・`.env` 等も参照していません。行番号は `24dacfb` 時点です。

## 1巡目の判定

- **X-37 — 閉鎖（実装）**
  - web は既定でサービス認証を有効にし、vault と agents の URL ごとに audience を分けて ID トークンを付けています。[service_auth.py:99](/Users/toshixa/dev/tenshokuagent/src/web/service_auth.py:99) [app.py:193](/Users/toshixa/dev/tenshokuagent/src/web/app.py:193)
  - IAM、`--no-allow-unauthenticated`、ingress は既存の I-7 デプロイ確認事項です。未確認ですが、新規指摘には数えません。
  - ただし、トークン取得後の audience 検査と失敗時のキャッシュ破棄には、後述の新規欠陥があります。

- **X-38 — 閉鎖**
  - web は `stages` の欠落フィールドを拒否し、さらに vault が正本の `mode in {demo, attack}` と架空候補者を検査しています。[stages.py:92](/Users/toshixa/dev/tenshokuagent/src/web/stages.py:92) [api.py:267](/Users/toshixa/dev/tenshokuagent/src/web/api.py:267) [store.py:1078](/Users/toshixa/dev/tenshokuagent/src/vault/store.py:1078)

- **X-39 — 残る**
  - base64url とデコード後32バイト以上の検査は入りましたが、乱数性は検査していません。[session.py:56](/Users/toshixa/dev/tenshokuagent/src/web/session.py:56) [session.py:70](/Users/toshixa/dev/tenshokuagent/src/web/session.py:70)
  - 例えば `SESSION_SIGNING_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` は32個のゼロバイトとして受理されます。既知の値なのでクッキーを偽造できます。
  - Secret Managerで所定のコマンドから生成するという台帳上のデプロイ前提を、生成手順・プロビジョニングで強制できるまで未閉鎖です。

- **X-40 — 残る**
  - web の明示ログ、タスク名、httpx INFO、web の Uvicorn アクセスログは改善されています。[referee.py:318](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:318) [app.py:178](/Users/toshixa/dev/tenshokuagent/src/web/app.py:178)
  - しかし vault には本番起動口もマスク処理もなく、`/v1/principals/{pid}` や `/v1/negotiations/{nid}` が Uvicorn のアクセスログに残ります。[vault/app.py:44](/Users/toshixa/dev/tenshokuagent/src/vault/app.py:44) [ledger.md:204](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/ledger.md:204)
  - Cloud Run基盤ログは既知の I-9 なので、ここでは重ねていません。

- **X-41 — 残る**
  - `message.metadata` と `params.metadata` は閉じましたが、`parts[0]` では content の種類と `data` しか検査していません。[validation.py:103](/Users/toshixa/dev/tenshokuagent/src/agents/validation.py:103)
  - 有効な `data` に `parts[0].metadata={"principal_instruction":"実年収620万円"}`、任意の `filename`、誤った `mediaType` を付けても通過してLLMが起動します。自由文はLLM入力には入りませんが、A2A層まで到達します。
  - `nid` 省略可は台帳で決定済みなので、指摘していません。

## 新しく入った欠陥

### 1. 実装 / medium — 誤った audience のトークンを最大約55分使い回し、401/403から回復しない

- **破綻シナリオ:** vault用に `audience=https://vault.example` を要求した際、メタデータサーバが `{"aud":"https://agents.example","exp":現在+3600}` のJWTを返す。実装は `exp` だけを読み、vault用キャッシュへ保存する。vaultは401/403で拒否するがキャッシュは破棄されず、更新余裕5分に入るまで約55分、同じ誤トークンが送られる。交渉だけでなく本人削除も進まなくなる。認可はfail-closedなので、直接の情報漏えいではなく可用性・削除保証の破綻です。
- **該当箇所:** [service_auth.py:63](/Users/toshixa/dev/tenshokuagent/src/web/service_auth.py:63)、[service_auth.py:99](/Users/toshixa/dev/tenshokuagent/src/web/service_auth.py:99)、[service_auth.py:135](/Users/toshixa/dev/tenshokuagent/src/web/service_auth.py:135)
- **直し方の方向:** キャッシュ前に未署名claimの `aud` が要求値と完全一致すること、`exp` が有限かつ未来であることを検査する。不一致はキャッシュしない。401/403では該当audienceのキャッシュを破棄し、取得し直して最大1回だけ再送する。

### 2. 実装 / low — 未知metadataのキー名に埋めた秘密値がエラーへ転記される

- **破綻シナリオ:** `message.metadata={"nid":"0123456789abcdef","実年収620万円":"x"}` を送る。Pythonの `\w` は日本語を安全文字として残すため、応答の `InvalidParamsError` の message/data に `message.metadata.実年収620万円` がそのまま入る。LLMは動きませんが、「入力値をエラーへ含めない」という規則に反し、エラーを記録する層にも残り得ます。
- **該当箇所:** [validation.py:35](/Users/toshixa/dev/tenshokuagent/src/agents/validation.py:35)、[validation.py:43](/Users/toshixa/dev/tenshokuagent/src/agents/validation.py:43)、[validation.py:61](/Users/toshixa/dev/tenshokuagent/src/agents/validation.py:61)
- **直し方の方向:** 未知キーの実名を返さず、`message.metadata.<unknown>`、件数、固定エラーコードだけを返す。Pydanticの余分なフィールドについても、利用者が作れるキー名を例外・ログへ載せない。

デモ読み出しと、vault/web双方の削除差分には、既存の決定事項を除いて新たな欠陥は見つかりませんでした。

