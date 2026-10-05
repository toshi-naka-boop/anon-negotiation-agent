> critic: codex/gpt-5.6-sol reasoning effort xhigh（558 秒。ログは round-9-security-codex.log）— 反証 1 巡目（安全性）。指摘の ID は台帳で X-37〜X-41 を振る

静的反証の結果は、high 1件・medium 3件・low 1件です。テストは実行せず、秘密領域も参照していません。v10 の SHA-256 は承認記録と一致しました。

1. 種別: 実装 / 重大度: high  
   一行タイトル: サービス間認証が未実装で、安全に動作するデプロイ形がない

   - 破綻シナリオ: vault を公開すると、攻撃者が `DELETE /v1/principals/<被害者pid>` や `POST /v1/negotiations/<nid>/control` を直接呼び、任意の依頼者を削除・交渉を取消できる。Cloud Run IAM で非公開にすると、現在の `httpx` クライアントは ID トークンを付けないため web→vault、web→agents が拒否される。
   - 該当箇所: [web/vault_client.py:88](/Users/toshixa/dev/tenshokuagent/src/web/vault_client.py:88)、[agents/client.py:43](/Users/toshixa/dev/tenshokuagent/src/agents/client.py:43)、[vault/app.py:86](/Users/toshixa/dev/tenshokuagent/src/vault/app.py:86)、[web/app.py:140](/Users/toshixa/dev/tenshokuagent/src/web/app.py:140)
   - 直し方の方向: vault・agents を認証必須かつ内部 ingress にし、呼出先 URL を audience とする短命 ID トークンを毎回付与する。欠落・誤 audience・誤サービスアカウントを拒否する検査を入れる。これは台帳 I-7 に既出だが、未解決のため現時点ではリリース阻害事項。

2. 種別: 実装 / 重大度: medium  
   一行タイトル: デモ読取の「本物ではない」判定が web 側の補助文書だけに依存する

   - 破綻シナリオ: 本物の交渉 `nid=N` に対する `stages/N` が、移行・障害復旧・古い文書などで `candidate_principal_id` 欠落または `null` になる。未認証の `GET /v1/demo/negotiations/N/events?side=employer` は架空交渉と判定され、vault は交渉の `mode` や参加者を再確認せず、本物の利用者側のイベントを返す。
   - 該当箇所: [web/stages.py:92](/Users/toshixa/dev/tenshokuagent/src/web/stages.py:92)、[web/api.py:265](/Users/toshixa/dev/tenshokuagent/src/web/api.py:265)、[vault/app.py:114](/Users/toshixa/dev/tenshokuagent/src/vault/app.py:114)
   - 直し方の方向: デモ読取専用の vault API を作り、vault の正本で `mode in {demo, attack}` かつ候補者が架空人物であることを検査する。`stages` の判定は補助防御に留め、欠落フィールドは必ず拒否する。

3. 種別: 前提 / 重大度: medium  
   一行タイトル: セッション署名鍵は非空でさえあれば弱い値でも受理される

   - 破綻シナリオ: `SESSION_SIGNING_KEY=a` でも起動する。攻撃者は `/start` で得た自分の署名付きクッキーから鍵をオフライン総当たりし、既知の被害者 PID と将来の `exp` を署名する。偽造クッキーと `X-Requested-With` を付ければ、被害者として閲覧・取消・削除できる。署名内の期限は偽造者が更新できるため防御にならない。
   - 該当箇所: [web/session.py:38](/Users/toshixa/dev/tenshokuagent/src/web/session.py:38)、[web/session.py:64](/Users/toshixa/dev/tenshokuagent/src/web/session.py:64)、[web/session.py:74](/Users/toshixa/dev/tenshokuagent/src/web/session.py:74)
   - 直し方の方向: Secret Manager 側で最低32バイトの暗号学的乱数を生成し、起動時に長さ・エンコードを検査する。鍵ローテーション用に現行鍵と旧検証鍵を分離し、短すぎる鍵では起動を拒否する。

4. 種別: 実装 / 重大度: medium  
   一行タイトル: 交渉 ID が通常ログに残り、本人削除後も相関可能である

   - 破綻シナリオ: `nid=N` の交渉で LLM が不正な出力を返す、または見回りが失敗すると、INFO/ERROR ログへ `N` が直接書かれる。レフェリーのタスク名にも `N` が入り、例外時に記録される。本人が削除して Firestore を空にしてもログは消えず、ログ閲覧者は交渉の時刻・失敗理由・側を継続して相関できる。
   - 該当箇所: [web/referee.py:239](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:239)、[web/referee.py:246](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:246)、[web/referee.py:311](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:311)、[web/referee.py:329](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:329)、[web/sweeper.py:107](/Users/toshixa/dev/tenshokuagent/src/web/sweeper.py:107)
   - 直し方の方向: PID/NIDをログから外す。必要ならログ専用の短期・鍵付き疑似IDに変換し、保持期間を限定する。アクセスログの ID 入り URL も同じ方針で伏せる。

5. 種別: 実装 / 重大度: low  
   一行タイトル: 壁1は DataPart を厳格検証するが metadata を閉じたスキーマにしていない

   - 破綻シナリオ: 正しい `TurnInput` に `message.metadata={"nid":"0123456789abcdef","principal_instruction":"実年収620万円"}` や任意の自由文フィールドを追加して送る。`nid` の形式だけが検査され、余分な metadata は拒否されず LLM が起動する。現状の executor は metadata を LLM 入力へ転記しないため直接の LLM 漏えいはないが、自由文は A2A スタックまで到達し、DEBUG 本文ログが有効なら記録対象になる。
   - 該当箇所: [agents/validation.py:59](/Users/toshixa/dev/tenshokuagent/src/agents/validation.py:59)、[agents/validation.py:91](/Users/toshixa/dev/tenshokuagent/src/agents/validation.py:91)、[agents/executor.py:77](/Users/toshixa/dev/tenshokuagent/src/agents/executor.py:77)
   - 直し方の方向: metadata を「必須の `nid` だけ」の型として検証し、未知キーを拒否する。request-level metadata は空に限定するか、message側の `nid` と完全一致させる。DataPart の media type も確認する。

それ以外は、コード上では次を確認できました。

- 生のアンカーは web で丸められ、vault の `Package`・`Anchor` はグリッド値しか受けないため、丸め前の数値が vault や交渉 LLM に入る直接経路は見つかりませんでした。
- `X-Requested-With` はセッションや Firestoreへ触る前に検査され、現状は CORS 許可もないため、通常のクロスサイトフォームによる状態変更は遮断されています。
- クッキーの HMAC-SHA256署名、署名内 `exp` のサーバ側検査、HttpOnly・Secure・SameSite=Lax・29日という構成自体は設計どおりです。
- web の本人権限確認と依頼者ロック、削除順序は概ね設計どおりです。既知の I-8（冪等キー文書が削除後も残る）は台帳に未解決で記録済みなので、新規指摘としては重ねていません。

