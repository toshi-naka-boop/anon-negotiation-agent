critic: claude/claude-opus-5-5

## Round 18 — 2026-10-03 — 批評（設計書 v17。批評の間に v18（0fdebf6。codex の X-75〜X-79 の直し）が入ったので、下の指摘は v18 でも残ることを確かめた）

- 読んだもの: design.md（v17 = 306d891、v18 = 0fdebf6。冒頭の変更一覧・§0〜§1.2・§2.7・§3.1〜§3.4・§4.1・§4.4・§9・§10・§12）、ledger.md（X-75〜X-79 を含む）、archive/ledger-resolved.md の「批評 16・17・18 巡目」、reviews/round-17.md・round-17-codex.md・round-18-codex.md、research/tee-spike-contract.md（§0〜§18）、research/tee-spike.md（手順 0・A〜F、点ごとの合否、危ない点）、tests/manual/tee-spike.md（共通の前提・点 2）。裏取りに src/vault/tee/key_release.py・src/vault/models.py・src/negotiation_core/schema.py・tests/test_validation.py と、批評の間に入った d459168 の src/web/api.py・src/web/attested_transport.py・scripts/verify_attestation.py の該当箇所を読んだ。
- codex の X-75〜X-79（Policy Analyzer の範囲と期待集合、AC-22 の (g)(h)、一時的な失敗の上限、監査ログの無効化・自己除外、`PlanEnvelope`）と重なるものは出さない。下の 3 件は、v18 の直しを入れても残るもの。
- 公式文書の取得（GCP には触れていない）は、C-62 の前提（principalSet の範囲、プロバイダを作れるロール、JWKS のアップロード）と、C-63 のトークンの寿命だけ。URL は各指摘に付けた。

### 指摘

- [C-62] 種別: 設計 / 深刻度: medium
  - 指摘: §9 の 2 段目（鍵の排他性）と §10 の照合は「D の principalSet ＝ 検証済みのワークロード」を前提にしている。しかし principalSet と subject はプール単位の識別子で、プロバイダを区別しない。プールに足したプロバイダや、`attestation-verifier` の発行元（issuer・JWKS）を差し替えたプロバイダからも、同じ principalSet・同じ形の subject の主体を作れる。(a) は 1 つのプロバイダの mapping と condition しか見ず、(b) の Policy Analyzer は鍵に束縛された主体しか列挙しない。そのため、TEE を通らずに KEK を使う道が、照合にも主張の列挙にも入っていない。
  - 破綻シナリオ:
    1. プロバイダを作れる主体が、`vault-tee-pool` に 2 つ目の OIDC プロバイダを作る。例: `gcloud iam workload-identity-pools providers create-oidc evil --workload-identity-pool=vault-tee-pool --location=global --issuer-uri=https://evil.example --jwk-json-path=keys.json --attribute-mapping='google.subject=assertion.sub,attribute.image_digest=assertion.image_digest'`。鍵は `--jwk-json-path` で上げるので、発行元を公開する必要もない。作れるのは Owner・`roles/iam.workloadIdentityPoolAdmin`・`roles/iam.admin` などで、Editor にはない（公式の権限表）。
    2. 自分の鍵で JWT `{sub: "gcpcs::<D>::<番号>::<本番の VM の ID>", image_digest: "<D>"}` を作り、STS で `.../providers/evil` に交換する。得た連携トークンは `principalSet://.../vault-tee-pool/attribute.image_digest/<D>` に含まれるので、`_tee/dek` の `wrapped_dek` を `:decrypt` できる。
    3. 既存の `attestation-verifier` の `--issuer-uri`・`--jwk-json-path` だけを `update-oidc` で差し替え、mapping と condition の文字列は変えずに行っても同じ（condition が見る claim は、すべて自分の JWT に書ける）。用が済んだら戻すか消す。
    4. (a) は mapping と condition が本番の文字列なので通る。(b) は鍵の束縛（principalSet D と承認済みのオーナー）しか列挙しないので通る。(c)〜(h) も変わらない。
    5. Cloud KMS の Data Access ログに残る主体は、オーナーのメールではなく、金庫が起動時に復号するときと同じ形の `principal://.../vault-tee-pool/subject/gcpcs::<D>::…` になる（推測: 監査ログの主体の欄にプロバイダは入らない）。v18 の「監査の設定が有効で除外がない間は、復号が…主体と時刻つきで残る。オーナーは監査の設定・鍵の IAM・鍵の版を変えられるが…」は、監査の設定を一切変えずに、記録の上の主体を金庫に見せかけられるので成り立たない。残るのは Admin Activity のプロバイダの作成・更新だけで、これは主張の列挙にも照合にもない。
    6. このプロジェクトでいまプロバイダを変えられるのはオーナーだけだが、プールの管理者のロールを誰かに付けた時点で（またはその資格情報が漏れれば）、2 段目の「ほかの管理者・漏れた資格情報には復号権がない」が、照合を通ったまま偽りになる。
  - 根拠（公式）:
    - principalSet・subject がプール単位であること（「All identities in a workload identity pool with a certain attribute」の識別子にプロバイダがない）: https://cloud.google.com/iam/docs/principal-identifiers
    - `iam.workloadIdentityPoolProviders.create`・`.update`・`iam.workloadIdentityPools.update` を含むロール: https://cloud.google.com/iam/docs/roles-permissions/iam
    - `--jwk-json-path`・`--issuer-uri`: https://cloud.google.com/sdk/gcloud/reference/iam/workload-identity-pools/providers/create-oidc 、同 `update-oidc`
  - 提案:
    - (a) をプールの全体の完全一致にする: 有効なプロバイダが `attestation-verifier` の 1 件だけ。その `issuerUri` が `https://confidentialcomputing.googleapis.com/`、`jwksJson` が空、`allowedAudiences` が `https://sts.googleapis.com` だけ、`disabled=false`、mapping と condition が本番の文字列。プール自体も有効。
    - (b) に、プールとプロバイダを変えられる権限（`iam.workloadIdentityPoolProviders.create`・`.update`・`iam.workloadIdentityPools.update`）を持つ主体の Policy Analyzer の列挙を足し、期待値は `expected-kms-principals.json` のオーナーだけにする。
    - §9 のオーナーの限界の列挙に「WIF のプール・プロバイダ」を足し、「WIF を経由した復号は、Data Access ログにオーナーのメールではなく、プールの主体（金庫と同じ形にも作れる）として残る」と書く。あわせて「記録を読めるのは、このプロジェクトの権限を持つ者（運営者自身）だけで、第三者は確かめられない」も説明文に書く（「抑止」が誰に効くのかを正直にするため）。
    - 組織の配下なら（P-13）、組織のポリシー `constraints/iam.workloadIdentityPoolProviders` で発行元を Confidential Space だけに縛れる（研究報告 R23 がすでに触れている）。

- [C-63] 種別: 設計 / 深刻度: medium
  - 指摘: C-59 の直し（権限を外して 65 分待ってから版を回す）は、研究報告・手動の手順の D の 1 回目の切り替えには効くが、次の 3 点が残る。
    1. §9 の順序と契約 §16・§17 には「プロバイダを本番の条件に更新する」段がない。65 分の根拠（「トークンは最長 1 時間で切れる」）は、更新が待ちより前にあるときだけ成り立つ。
    2. 待ちが効いたか（置かれた DEK がないか）を確かめる段がない。手動の手順は `sealing self-test ok` を「新しい DEK が作られた」証拠にしているが、置かれた DEK でも同じ行が出る。
    3. 本番に切り替えた後に debug の条件へ戻すときの規則がない。10/5 以降の R1（`store.py` への封印の組み込み）を §9 の 1 のとおり「先に debug イメージで通す」と、debug の VM が今の DEK を開く。§9 の「言えないこと」は「debug の間に作った DEK」しか扱っていない。これは C-59 の原文の後半（「同じ穴は、本番に切り替えた後にも開く」）で、v17 の直しが扱わなかった部分。
  - 破綻シナリオ:
    1. §9 の順序どおりに進め、プロバイダの更新を本番の VM の起動の直前に回した場合: debug の VM の root は、削除の直前に launcher から新しい attestation トークン T（audience は `https://sts.googleapis.com`。公式: 1 時間有効）を取って控える。プロバイダはまだテスト用の条件なので、削除から 55 分後に T を STS で交換すると、連携トークン F は交換の時刻から最長 1 時間使える（推測: STS の文書は `expires_in` を返すと書くだけで、元のトークンの `exp` で切り詰めるとは書いていない）。65 分後に権限を付け直して V2 を primary にした時点で F はまだ有効なので、F で自分の DEK_x を V2 で包み、`_tee/dek`（`kek_version`=V2）を置ける。
    2. 本番の金庫は初回の起動で `_tee/dek` を見つけ、探りの encrypt の `name`（V2）と一致するので解く（ログは `key release: unwrapped the stored DEK`）。`_tee/selftest` は消してあるので DEK_x で作り直し、`sealing self-test ok` を出す。手動の手順の 7・10 と (h) は「self-test ok」と「`kek_version` が primary」しか見ないので、すべて通る。オーナーが自分の権限の encrypt で DEK_x を置いた場合も同じで、復号を一度もしないので v18 の「復号が記録に残る」にも当たらない。
    3. 10/5 以降、R1 の新しいダイジェストを debug イメージで先に通すため、プロバイダをテスト用の条件に戻し、束縛を足す。debug の金庫は手順 D で作った `_tee/dek` を開き、root から DEK が見える。§9 の規則（debug の間に「作った」DEK は作り直す）には当たらないので、規則の上では、R1 は同じ DEK で live のデータを封印してよいことになる。テスト用の条件の間は、1 の連携トークンの道も開き直す（版を回す手順は、1 回目の手順 D にしか書かれていない）。
  - 根拠（公式）: attestation トークンは 1 時間（「This token lasts one hour, and is automatically refreshed」）: https://docs.cloud.google.com/confidential-computing/confidential-space/docs/create-grant-access-confidential-resources 。STS の応答の `expires_in`: https://cloud.google.com/iam/docs/reference/sts/rest/v1/TopLevel/token
  - 提案:
    - §9 の順序と契約 §17 の先頭に「プロバイダを本番の条件に更新する」を置き、待ちはその後に権限を外した時刻から数える、と書く（研究報告 D の順序に揃えるだけ）。
    - 待ちが効いたことを記録で確かめる: (g) に「V2 を作った後の Cloud KMS の Data Access ログで、成功した Encrypt・Decrypt の主体が本番の VM の subject（`gcpcs::<D>::<番号>::<本番の VM の ID>`）だけ」を足す（17 巡目の C-59 の提案の残り）。debug のトークンで置けば debug の VM の ID が、オーナーが置けばオーナーのメールが出る（C-62 の直しで subject の偽装も塞がる）。補助に、手動の手順 7・10 の合格を「DEK を消した後の最初の起動のログに `key release: created a new DEK and stored it wrapped` がある」にし、「self-test ok = 新しい DEK」の書き方を直す（ログの行は logWriter を持つ主体が書き足せるので、主には Data Access ログで見る）。
    - 規則を足す: 「プロバイダを debug の条件にした期間があれば、本番に戻すたびに手順 D の全体（条件の更新 → debug の VM の削除 → 権限を外して 65 分 → 版を回す → DEK を作り直す）をやり直す。live のデータが入った後は debug の条件に戻さない（調べは本番イメージのログで。debug が要るなら、別のプール・別の鍵・別のデータベースで）」。§9 の「言えないこと」も「debug の間に作った、または開いた DEK」にする。

- [C-64] 種別: 前提 / 深刻度: medium
  - 指摘: 計画の寛容な読み（v16 の L16-1 から v18 の `PlanEnvelope` まで。`checks` が有効なら `move`・`package` を型検証せずに捨て、第 1 段は `schema`・`checks` 以外の項目を受け流す）は、AC-04 と両立しない。AC-04 は、設計書 §12.1 では「未定義の項目・グリッド外の値…が全受信口とレフェリーの両方で拒否される」、spec では「…を含むメッセージが、受信側ですべて捨てられる」。DV-17 と AC-04 の両方を満たす実装はなく、spec の完了条件の読み替えになるので、ユーザーの判断が要る。
  - 破綻シナリオ:
    1. エージェントが `{"schema":"plan/v1","checks":[P],"move":"propose","package":{"salary":310,…},"note":"…"}` を返す。JSON モードで応答スキーマの縛りがないので、余分な項目もグリッド外の値も出うる。
    2. ③ で §2.7 どおりに作ると、第 1 段は `note` を受け流し、`checks` が空でないので `package`（グリッド外）を捨て、P を確かめる（評価を 1 消費）。DV-17 の「`checks` が有効なら…グリッド外があっても計画は有効」には合う。AC-04 は、この計画をレフェリーで拒否することを求める。拒否する実装は、逆に DV-17 で落ちる。
    3. `checks` が空の計画でも、`move`・`package` 以外の項目（`note`）を第 2 段で見るとは書いていない（第 1 段で受け流すとだけある）。いまの `tests/test_validation.py::test_plan_rejects_undefined_field` が守る「未定義の項目は拒否」が、レフェリーの経路では効かなくなりうる。
    4. AC-04 の判定は `tests/test_validation.py` で、レフェリーが使わなくなる `Plan` 型を直接検証している。③ の後も緑のまま通り、spec の完了条件を満たしたと報告される（空振り。fix-1w で一度直した種類）。
  - 提案（ユーザーに聞く）:
    - 案 1（推奨）: 計画の出力に限って AC-04 を読み替える。「`checks` が有効なら、`move`・`package`・未定義の項目は、メッセージごとではなく項目として捨てる（FR-17 の『捨てる』を項目の単位で読む）。捨てた値は、履歴・`TurnInput`・ログ・相手のどこにも渡らない（FR-16 の、相手に自由文字列が届かない目的は保つ）。`checks` が空なら、未定義の項目を含む計画は `schema_invalid`」。AC-04 の試験は、`Plan` 型ではなくレフェリーの 2 段の読みを通して書き直す。
    - 案 2: 寛容な読みをやめ、AC-04 の文言どおり計画全体を `schema_invalid` にする（C-51・L16-1 の、手がかりのない繰り返しが戻る）。
    - どちらでも、第 1 段の「ほかの項目は受け流す」の範囲（`move`・`package` だけか、すべてか）を書く。

### low のメモ（件数に数えない）

- L18-1（設計 / X-79 の取りこぼし）: v18 の §2.7 に旧い記述が 2 か所残る。「出力の形の制約」の「レフェリーが pydantic の `Plan`・`Move`（strict、グリッド値の列挙）で検証する。違反は `schema_invalid`（形）・`off_grid`（グリッド外）の無効手」と、`Move` の「グリッド外の値は、レフェリーの検証で `off_grid` の無効手になる」。台帳は X-79 を解決済みにしている。コードでは `LastErrorReason` に `off_grid` が残り（`src/negotiation_core/schema.py`）、指示文の 2 本も挙げる。いまのレフェリーはグリッド外を `schema_invalid` で登録しているので、実害はない。
- L18-2（設計 / X-78 の補足）: v18 §9 は「設定が有効で除外がない間…(g) で確かめる」とするが、(g) は「DATA_READ・DATA_WRITE が有効」しか見ない。`auditConfigs` の `exemptedMembers` が空であること（`cloudkms.googleapis.com` と `allServices` の両方）を (g) に足す。記録が `_Default` のバケットの既定で 30 日で消えることも説明文に書く。
- L18-3（手順書）: 手動の手順 点 2 の「合格の基準」に「負の試験 3 つ(…手元のオーナー)が拒否される」が残る（C-58 で替えた）。同じ点の「合格の出力」は「旧版を無効化したあとは…ログは `non-primary key version` ではなく KMS のステータス」とするが、実装（`_unwrap`）は復号の前に版を照合するので、出るのは `non-primary key version`（手順 9 の記述が正しい）。研究報告 R7 の「Data Access ログを有効にすれば…（任意）」は (g) の必須と食い違う。手順 A は impersonate の権限を「F で外す」とするが、F にそのコマンドがない（`serviceAccountOpenIdTokenCreator` の 2 つも同じ）。
- L18-4（契約）: 契約 §16 の C-56（`GET` で primary を読む、`rotate the DEK` の固定文）と C-57（失敗したらピンを外す）の文は §17・§18 で置き換わったが、§16 の本文に印がなく、§17・§18 の見出しにも「§16 より優先」とない。§16 だけを読んだ実装者は古い振る舞いを作る。
- L18-5（設計）: 公開の `nonce` の転送は全体で 10 秒に 1 回で、利用者ごとの制限がない（d459168 の `_TeeAttestationEndpoint` も全体の 1 枠）。匿名の 1 人が 1 秒ごとに呼び続ければ、枠が空いた直後をほぼ毎回取れ、審査員の AC-23 `--web` はほとんど 429 になる（スクリプトは再試行せず「少し待ってから、もう一度」と出す）。§8.2 の IP ごとの枠を転送にも掛け、スクリプトは `Retry-After` に従って数回やり直す。
- L18-6（設計）: §4.1 の「呼び出し数の見積もり」に「交渉ごとの上限 36 で抑える」が残る（v14 から 44）。
- L18-7（設計）: §9 の「言えないこと」の「10 分ごと…その間に失効したイメージが動き続けることはあり得る」が、v18 の 30 分（`max_unverified_seconds`）を反映していない。公式は、失効したイメージでは `swname` が `GCE` になるか、トークンの要求が `RIMS_VALIDATION_FAILED` で失敗すると書く（C-63 の Confidential Space の URL）。後者は金庫の 503（一時的な失敗）になるので、業務の要求が止まるまで最長で約 40 分（10 分 ＋ 30 分）。上限を数で書く。

### 問題なしとした観点（根拠と、最も危うい前提）

1. 手順 D（研究報告・手動の手順）の順序なら、65 分の待ちで debug の連携トークンの道は閉じる。
   - 研究報告 D と手動の手順は、プロバイダを本番の条件に更新してから VM を消し、権限を外して待つ。条件は交換のときに評価されるので、更新の後は debug のトークンを交換できない。更新の前に交換した連携トークンは最長 1 時間（codex が公式で確認）、attestation トークンも 1 時間（公式）。
   - 最も危うい前提: プロバイダの更新の反映の遅れが、待ちの余裕（5 分 ＋ 負の試験 1 と VM の削除にかかる時間）より短いこと。§9 がこの順序を書いていない点は C-63。
2. 「否定の結果だけで外す」と v18 の 30 分で、C-57 と C-61 の両方が満たされる。
   - 429・503 は一時的な失敗なので、匿名の転送で内部の再検証が外れることはない（2 秒後にやり直す）。否定のトークン（debug・失効・表にない・`swname=GCE`）は次の再検証（10 分以内）で外れ、503 が続く場合も最後の成功から 30 分で業務の要求が閉じる。Google の署名鍵を取れないときは、実装（d459168 の `attested_transport.py`）が一時的な失敗に分類しているので、Google 側の障害でピンは外れない。
   - 最も危うい前提: 503 が続く間（最長 30 分）は、検証の通らない金庫に送り続けること（X-77 で受け入れた上限。数で書く点は L18-7）。
3. C-60 の探りの encrypt。
   - `src/vault/tee/key_release.py` の `_unwrap` が、復号の前に探りの `name` と `kek_version` を照合し、`kek_version` のない文書も拒否し、固定文が DEK を消さないよう案内することを読んで確かめた。追加の IAM は要らない。
   - 最も危うい前提: primary の変更の反映の遅れ（通常 1 分）。最初の DEK が古い版で包まれても、手順 8 の再起動で止まり、live のデータがない段なので作り直しで戻せる。
4. §4.4・DV-11 の `last_check`。
   - §2.7・§3.1・§4.4 の定義、回答で P に置き換える規則、DV-11 の Q≠P のケースが揃っている。§4.1 の「履歴から埋める」の優先順位（`view` の `last_check` を履歴より先）とも矛盾しない。

### まとめ

- 新規の high はない。medium は 3 件（C-62・C-63 は設計、C-64 は前提）。low のメモは 7 件（L18-1〜L18-7）。
