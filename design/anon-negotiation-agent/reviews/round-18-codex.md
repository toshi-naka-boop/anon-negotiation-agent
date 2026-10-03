critic: codex/gpt-5.6-sol xhigh

新規 high はありません。新規 medium は 5 件です。したがって、指定された条件上は承認に進めます。なお、65 分待機と探りの `encrypt` 自体には新たな破綻を認めませんでした。Confidential Space の attestation token は 1 時間で失効し、WIF の token も最大 1 時間です。[Google Cloud](https://docs.cloud.google.com/confidential-computing/confidential-space/docs/create-grant-access-confidential-resources)

### X-75（種別: 設計 / 重大度: medium）Policy Analyzer の実行範囲と期待集合が確定していない

- **どこが**: [§10](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:967) のコマンドには必須の `--project`／`--folder`／`--organization` がなく、オーナー一覧の正本もない。一方、[契約 §16](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/research/tee-spike-contract.md:231) は期待集合からオーナーを落としている。Policy Analyzer は指定 scope 以下だけを解析する。[公式リファレンス](https://docs.cloud.google.com/sdk/gcloud/reference/asset/analyze-iam-policy)
- **破綻のシナリオ**: 契約どおりならオーナーが出て常時失敗する。安易に `--project` を足すと、組織・folder から継承した復号主体を見落とす。現在の IAM からオーナーを自動採取すると、不正に追加されたオーナーまで期待値となって合格する。
- **直し方の案**: 承認済みオーナーを含む期待 principal の正本を別ファイルに固定する。組織配下なら `--organization`、それ以外は `--project` を明示し、未完了解析も失敗にする。契約 §16 も同じ集合へ揃える。

### X-76（種別: 設計 / 重大度: medium）AC-22 が新設した監査・再起動検査を合格条件から落としている

- **どこが**: §10 は照合を (a)〜(h) としたが、[AC-22](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:1050) は依然として (a)〜(f) だけを要求する。
- **破綻のシナリオ**: 実装者が AC-22 を正として、Data Access 監査ログ (g) と、`kek_version`・強制再起動・既存暗号文の開封 (h) を省いた `deploy_check.sh` を作る。デプロイは合格するが、次の再起動で金庫が停止するか、オーナーの復号が記録されない。
- **直し方の案**: AC-22 を明示的に (a)〜(h) へ変更し、(h) は「再起動前に存在した `_tee/selftest` が再起動後に開く」ことまで合否に書く。

### X-77（種別: 設計 / 重大度: medium）一時的失敗なら古いピンを無期限に信用できる

- **どこが**: [§9](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:897) と [契約 §17](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/research/tee-spike-contract.md:238) は、429・503・接続失敗ではピンを保持してデータ通信を続けるが、最終成功時刻の上限を定めていない。
- **破綻のシナリオ**: 一度検証された金庫が、その後 `/v1/attestation` だけを 503 にし続ける。再検証は 2 秒ごとに失敗する一方、業務 API には秘密が無期限に送られ、「10 分ごとに再検証」の実質的な保証が消える。
- **直し方の案**: ピンの削除とデータ通信の許可を分ける。一時的失敗では証明書を保持してよいが、`last_verified_at` または token の `expires_at` を越えたら業務要求を閉じ、attestation だけを再試行する試験を追加する。

### X-78（種別: 設計 / 重大度: medium）オーナーの復号が必ず主体・時刻つきで残るとは言えない

- **どこが**: [§9](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:905) は、オーナーが復号すれば Data Access ログに主体と時刻が残ると断定する。しかしプロジェクトの IAM を変更できるオーナーは、Data Access ログを無効化したり、自分を除外対象にしたりできる。[Google Cloud の仕様](https://docs.cloud.google.com/logging/docs/audit/configure-data-access)
- **破綻のシナリオ**: オーナーが KMS の Data Access ログを一時的に無効化または自己除外し、DEK を復号してから設定を戻す。設定変更の Admin Activity は残るが、復号そのものの主体・時刻は残らず、説明文の主張が破れる。
- **直し方の案**: 「監査設定が有効で除外がない間は復号を記録できる。オーナーは設定を変えられ、その変更記録による抑止まで」と弱める。強い保証が必要なら、組織側で監査設定を継承させ、オーナーが管理できない別プロジェクトへログを送る。

### X-79（種別: 設計 / 重大度: medium）`Plan` の二段読みと旧来の一括 strict 検証が併存している

- **どこが**: [§2.7](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:321) は二段読みを定めた直後、次行と `Move` の説明で再び「strict な `Plan` 型」「`off_grid`」を要求している。[§4.1](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:545) も二段読みを明記せず、受信済みの `Plan` として扱う。
- **破綻のシナリオ**: 実装者が後続の説明に従って `Plan.model_validate()` を先に呼び、有効な `checks` と不正な無視対象 `move/package` を含む応答を `schema_invalid` にする。DV-17 が要求する寛容な読みと逆になる。
- **直し方の案**: 第1段を別型（例: `PlanEnvelope`）として明記し、§4.1 に「Envelope 検証→checks があれば残りを未検証で破棄→空なら Move 検証」を書く。`off_grid` と「strict な Plan 型」の残存記述をすべて `schema_invalid`／二段読みへ揃える。

