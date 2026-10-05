critic: codex/gpt-5.6-sol xhigh

### X-70（種別: 設計 / 重大度: high）鍵版の切替順序で金庫が再起動不能になる

- **どこが**: §9 は `_tee/dek.kek_version == primary` を起動条件にしながら、本番イメージへ切り替えた「直後」に primary を更新する。契約 §5 には `kek_version` の保存・照合自体がなく、§10(d)・AC-22 も `_tee/dek` や再起動を検査しない。
- **破綻のシナリオ**: 本番金庫が旧版 V1 で DEK を包んで起動した後、V2 を primary にして V1 を無効化する。稼働中はメモリ上の DEK で動くため AC-22 は通るが、次回起動では版不一致で停止する。そこで DEK を作り直すと、既存の封印済みデータを開けなくなる。
- **直し方の案**: live データ投入前に「debug 停止 → V2 を primary → V1 無効化 → debug の DEK 削除 → 本番を初回起動」の順にする。`_tee/dek.kek_version == V2`、強制再起動、既存暗号文の開封を AC-22 に加える。live 後の版更新は DEK の再生成ではなく、同じ DEK の再ラップ手順を定める。

### X-71（種別: 設計 / 重大度: high）deploy_check は「ほかの主体に復号権がない」を証明しない

- **どこが**: §10(b) は KEK 自身の IAM、(c) は `web`・VM の SA のプロジェクト／鍵上のロールだけを見る。§9 の主張に必要な WIF の attribute mapping、key ring や folder・organization からの継承、custom role を含む実効的な KMS 権限は照合対象にない。
- **破綻のシナリオ**: key ring または上位階層で別主体に復号権限が付いていても、(a)〜(f) と AC-22 は合格する。その主体は TEE を経由せず KEK を使用できるため、「D の principalSet だけ」という第2段の主張が破れる。
- **直し方の案**: attribute mapping と condition を組で完全一致検査し、`cloudkms.cryptoKeyVersions.useToDecrypt/useToEncrypt` の実効権限を全階層・custom role 込みで列挙して、期待する principalSet 以外が一件でもあれば失敗させる。そこまで検査しないなら、主張を「CryptoKey 直下の binding が一致する」まで弱める。

### X-72（種別: 設計 / 重大度: medium）失効と定期再検証が実装契約に存在しない

- **どこが**: v16 §9 は `status=revoked` と10分ごとの再検証を要求するが、契約 §10・§13 の releases 形式には `status` がなく、全 digest が `allowed_digests` に入る。契約 §8 は接続エラー時しか再検証せず、§15 の single-flight も初回・接続エラーだけである。追記しかできない `tee_record_release.py` に失効操作もない。同様に、L16-5 の `format=full` も契約 §7・§12には固定されていない。
- **破綻のシナリオ**: R0 をリポジトリ上で revoked にしても、稼働中の `web` は古い許可集合と既存の TLS 接続を使い続け、接続エラーがなければ永久にデータを送る。契約どおりに ID トークンを取得して `email` が欠ければ、逆に全 API が 403 になる。
- **直し方の案**: 契約の型・スクリプト・transport を更新し、`active` だけを許可する。許可表の版と再読込方法を定め、10分経過後の最初の要求またはバックグラウンド処理で共有 single-flight を通して再検証する。時計を進めて `active→revoked` に変え、ピン解除後に金庫呼出しが止まる試験を加える。

### X-73（種別: 設計 / 重大度: medium）AC-23 の `--web` は金庫証明書を独立に照合できない

- **どこが**: AC-23 は `--web`・`--direct` の双方で、観測した金庫証明書のハッシュを照合するとする。一方、契約 §10 は `--web` では `certificate_sha256=None` として検査を飛ばし、§12 のスクリプトも公開 web API を呼ぶだけで金庫の TLS 証明書を観測できない。これは「web がその TEE にだけ送っている証明ではない」とする §9 とも食い違う。
- **破綻のシナリオ**: `web` が許可された別 TEE から得た正しい JWT を返しつつ、実際の業務通信を別経路へ送っても `--web` は成功する。受入条件だけ読むと、通信経路まで結び付いたように誤認される。
- **直し方の案**: `--web` は nonce・署名・claims・active digest の検証だけ、`--direct` だけが観測した金庫証明書との結合を検証する、と分離する。`--project`・`--service-account` は CLI と本番用 `AttestationPolicy` の双方で省略不能にする。

### X-74（種別: 設計 / 重大度: medium）`checks` 優先の寛容な読みが strict な Plan 検証より後にある

- **どこが**: §2.7 は `Plan` を strict な pydantic 型として検証するとしながら、有効な `checks` があれば列挙外の `move="check"` やグリッド外の `package` も無視するとする。§4.1 には全体検証より先に `checks` だけを取り出す手順がなく、通常のモデル検証では無視する前に Plan 全体が失敗する。さらに §2.7・`Move` には、廃止したはずの `off_grid` が残っている。
- **破綻のシナリオ**: LLM が有効な checks と余分な不正 move を返すと、DV-17 が期待する checks の実行ではなく `schema_invalid` になる。同じ JSON を繰り返せば無効手を消費して交渉が停止する。
- **直し方の案**: JSON object と `schema`・`checks` を先に検証し、非空なら raw の `move`・`package` を型検証せず破棄する二段階 parserを設計する。`checks=[]` の場合だけ `Move` を strict に検証する。`off_grid` は本文・型・試験からすべて除き、対象ケースをその parser に対する試験にする。

