critic: claude/claude-opus-5-5

## Round 17 — 2026-10-03 — 批評（設計書 v16）

- 読んだもの: design.md（v16、558e9b4）、ledger.md（b7c4e90 の時点。codex の X-70〜X-74 を含む）、reviews/round-16.md・round-16-codex.md・round-17-codex.md、archive/ledger-resolved.md の「批評 16 巡目」、research/tee-spike-contract.md（§15・§16 を含む）、research/tee-spike.md（§2・§3、手順 A〜F。作業ツリーで書き換え中の手順 D を含む）。裏取りに tests/manual/tee-spike.md の負の試験 3 の行だけを grep した。
- codex の X-70〜X-74 と重なるもの（版の切り替えの順序と再起動、deploy_check の実効権限と属性の対応、契約に失効・定期の再検証・`format=full` がない、AC-23 の `--web`、`checks` 優先の読みと `off_grid` の残り）は出さない。下の 4 件は、その直し（契約 §16）を入れても残るもの。
- 実測: high の疑いの点を、公式文書の取得で確かめた（GCP には触れていない）。根拠の URL は各指摘に付けた。

### 指摘

- [C-58] 種別: 設計 / 深刻度: high
  - 指摘: プロジェクトのオーナーは、IAM を変えずに KEK で復号できる。`cloudkms.cryptoKeyVersions.useToDecrypt`・`useToEncrypt` は旧来の基本ロール `roles/owner` に含まれ、プロジェクトから鍵へ継承される。次のものは、すべて逆の前提に立っている。§9 の 2 段目（「KEK の権限は D の principalSet だけ…ほかの主体にも復号権はない」「鍵の IAM か鍵の版を変えない限り成り立つ」）。「封印が守るのは、運営者が Firestore を直接読む・エクスポートする・バックアップから読む場合」。研究報告 2-3 の「運営者の限界」。点 2 の負の試験 3（手元のオーナーは 403）。
  - 破綻シナリオ:
    1. 運営者は、手順 A の IAM を自分で付けたプロジェクトのオーナーで、Firestore の REST で `_tee/dek` を読む（書き換え中の手順 D も、負の試験のためにこれを手元に控える）。
    2. 手元の認証で `gcloud kms decrypt --keyring=vault-tee --key=vault-kek --ciphertext-file=<wrapped_dek>` を実行する。鍵の IAM・WIF の条件・鍵の版のどれも変えないので、Admin Activity には何も残らない（Decrypt は Data Access のログで、既定で無効）。
    3. 平文の DEK と公開したコード（AES-256-GCM、AAD は `path#field`）があれば、直接の読み取り・エクスポート・バックアップ（PITR を含む）から封印した全項目（ポリシー・コピー・イベントの payload・属性帯）を開ける。C-56・X-70・X-71 で積んだ版の照合・切り替えの順序・照合の網羅は、運営者に対しては何も守らない。
    4. スパイクでは、手動手順の負の試験 3（ダミーの暗号文で `gcloud kms decrypt`）が `INVALID_ARGUMENT`（権限の検査を通った応答）になる。手順書の基準では不合格、研究報告の基準では「負の試験で鍵が漏れる → 即、中止」になる。点 2 は必須（P-9 の推奨）なので、§9 の規則では Cloud Run 版に戻る。
    5. 契約 §16 の X-71 の照合は、Policy Analyzer で useToDecrypt・useToEncrypt を持つ主体を列挙し、active な principalSet 以外が 1 件でもあれば失敗とする。この照合は、オーナーが必ず出るので、この構成では常に失敗する。Policy Analyzer は拒否ポリシーを考慮しないので、下の (a) を入れても、この照合の形のままでは失敗する。
  - 根拠（公式）:
    - https://docs.cloud.google.com/iam/docs/roles-permissions/cloudkms の「Cloud Key Management Service permissions」の表: `cloudkms.cryptoKeyVersions.useToDecrypt` と `useToEncrypt` を含むロールに `Owner (roles/owner)` がある（`Editor` にはない）。
    - https://cloud.google.com/kms/docs/separation-of-duties: 暗号操作の権限をほかの主体から外すには、IAM の拒否ポリシーを使う。Security Command Center は「Project Owner を含め、管理と暗号の両方の権限を持つ主体」を検出する。
    - https://cloud.google.com/policy-intelligence/docs/policy-analyzer-overview: Policy Analyzer（allow）は拒否ポリシーを考慮しない。
  - 提案（点 2 がこれで落ちるため、手順 D の前に決める）:
    - (a) プロジェクトに IAM の拒否ポリシーを付ける。`cloudkms.googleapis.com/cryptoKeyVersions.useToDecrypt`・`useToEncrypt`（と `...ViaDelegation`）を `principalSet://goog/public:all` に対して拒否し、例外（exceptionPrincipals）は active なダイジェストの principalSet だけにする。KMS の権限も、WIF の `attribute.*` の principalSet も、拒否ポリシーで使える（https://cloud.google.com/iam/docs/deny-permissions-support 、https://cloud.google.com/iam/docs/principal-identifiers ）。
      - ただし、拒否ポリシーを管理する `roles/iam.denyAdmin` は組織にしか付けられない（https://cloud.google.com/iam/docs/roles-permissions/iam の「Lowest-level resources: Organization」）。プロジェクトが組織の下にあり、ユーザーが組織でこのロールを取れるかは、ユーザーへの質問になる。
      - deploy_check は Policy Analyzer の結果だけに頼らず、次の 2 つで照合する。拒否ポリシーの中身が完全一致すること。allow で useTo* を持つ主体（オーナーを含む）が、例外の principalSet を除いて、すべて拒否ポリシーの対象に入っていること。
    - (b) 組織がない場合と、ロールを取れない場合: オーナーの復号権は外せない。
      - §9 の 2 段目を「KEK は D の principalSet と、プロジェクトのオーナー（運営者）が使える」に弱め、封印が守る相手を「オーナー以外で Firestore を読める者」に直す。
      - KMS の Data Access の監査ログ（DATA_READ・DATA_WRITE）を有効にする。オーナーの Decrypt が記録に残ること（無効化は Admin Activity に残る）を、排他性の代わりの主張にする。
      - TEE をこの弱い版で続けるかは、P-9 とあわせてユーザーが決める。
    - (a)・(b) のどちらでも、次を直す。負の試験 3 の期待値。研究報告 2-3・2-8。§9 の但し書き。但し書きには、鍵の IAM と版に加えて、WIF のプロバイダ（条件・属性の対応。X-71 で照合を足したもの）を入れる。(a) を採るなら拒否ポリシーも入れる。

- [C-59] 種別: 設計 / 深刻度: high
  - 指摘: C-56 の直しは、「版を回せば、debug の間に作った包みは使えない」を前提にしている（`kek_version` を primary と照合し、本番に切り替えるときに版を回す。§16 では本番の初回起動の前に回す）。しかし、debug の VM で STS から得た連携トークンは、プロバイダの条件を本番に変えた後も、期限まで鍵を使える。鍵に付けた principalSet は、debug と本番で同じダイジェスト D だからだ。鍵の IAM と、C-58 の (a) の例外の両方が、そのトークンを通す。手順 D は条件を変えてから十数分で新しい版 V2 を作るので、運営者は V2 で自分の選んだ DEK を包める。この穴は、C-58 を (a) で直した後も残る。
  - 破綻シナリオ:
    1. 手順 C の debug の VM で、運営者は root として launcher のトークンファイル（`/run/container_launcher/attestation_verifier_claims_token`）を読む。手元で STS に交換して、連携トークン T を得る（テスト用の条件で通り、主体は `attribute.image_digest/D`）。
    2. 手順 D を §16 の順に進める（プロバイダを本番の条件に更新 → debug の VM を削除 → V2 を primary に → V1 を無効化 → `tee_reset_dek.py --yes`）。ここまで十数分で、T はまだ有効。
       - プロバイダの条件は、交換のときにだけ評価される。無効にしたプロバイダでさえ「既存のトークンはアクセスを与え続ける」（https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools.providers の `disabled`）。
    3. 運営者は T で `:encrypt` を呼び、自分が知っている DEK_x を V2 で包む（応答の `name` が V2 になるまで繰り返す）。本番の VM を作る前に、`_tee/dek` に `{wrapped_dek: C_x, kek_version: <V2 の名前>, …}` を書き、`_tee/selftest` は消えたままにする。
    4. 本番の金庫は `_tee/dek` を見つけ、`kek_version == primary` の照合を通す。C_x を開いて DEK_x で動き、selftest も DEK_x で作る。
       - 手順 D の確かめ（再起動で selftest が開く、古い V1 の文書を書き戻すと止まる）は、すべて通る。
       - 最後の「もう一度 DEK を消して再起動」の後も、C_x を書き戻せばよい。C_x は、V2 が有効な限りいつでも使える。
    5. Admin Activity に残るのは、計画どおりの操作だけ。Encrypt と Firestore の書き込みは Data Access のログで、既定で無効。deploy_check の (a)〜(f) も §16 の照合も通る。2 段目の「DEK を包んだ鍵の版が primary で、古い版は無効」は真なのに、DEK は運営者のもの。
    - 同じ穴は、本番に切り替えた後にも開く。R1（封印の組み込み。10/5 以降）を debug で調べるために条件をテスト用へ戻すと、debug の VM の root が D1 の principalSet で `_tee/dek` を開け、今の DEK が漏れる。§9 の「言えないこと」は「debug の間に作った DEK」しか扱っていない。
  - 提案:
    - 根本: 本番の鍵に付ける主体を、本番の条件を通ったトークンしか持てない属性にする。
      - 例: 属性の対応に `attribute.prod_digest` を足す。値は、`dbgstat == 'disabled-since-boot'` かつ `STABLE` のときだけダイジェスト、それ以外は `debug` にする。鍵と C-58 の (a) の例外は、`attribute.prod_digest/<D>` にだけ付ける。
      - debug の試験は、別の鍵（または鍵なし）で行う。これで、debug のトークンは時間に関係なく本番の鍵を使えない。
      - 推測: 属性の対応の CEL で条件式が書けるかは、スパイクで `update-oidc` して確かめる。
    - それができないときの最小:
      - プロバイダを本番にして debug の VM を消してから、連携トークンの寿命以上たってから V2 を作る。寿命は STS の応答の `expires_in` で、通常 1 時間と見ている（推測。スパイクで記録する）。
      - deploy_check の (d) を、「primary の版の `createTime` が、Admin Activity にあるプロバイダの更新時刻＋寿命より後」にする。
    - KMS の Data Access の監査ログを有効にする。deploy_check で、鍵を使った主体（`gcpcs::<D>::<番号>::<instance_id>`）が本番の VM だけであることを確かめる。
    - §9 の「言えないこと」を「debug の間に作った、または開いた DEK」に直す。あわせて、次の 2 点を足す。本番に切り替えた後に条件をテスト用へ戻したら、版の切り替えと DEK の作り直しをやり直す。live のデータを入れた後は戻さない。

- [C-60] 種別: 設計 / 深刻度: medium
  - 指摘: 契約 §16（C-56）は、金庫が起動時に `GET https://cloudkms.googleapis.com/v1/<鍵の名前>` で `primary.name` を読むとする。この API には `cloudkms.cryptoKeys.get` が要る。しかし金庫の principalSet に付けるのは `roles/cloudkms.cryptoKeyEncrypterDecrypter` だけで（手順 B。§10 の (b) は「ほかの束縛がない」）、このロールの権限は次の 5 つだけで、`cloudkms.cryptoKeys.get` は入っていない。
    - `useToDecrypt`・`useToEncrypt`・`locations.get`・`locations.list`・`resourcemanager.projects.get`

    あわせて、primary の変更の反映の遅れと、止まったときの復旧の案内が抜けている。
  - 破綻シナリオ:
    1. 手順 D で本番の VM を作る。`_tee/dek` がある起動（遅くとも、手順 D の「VM を再起動して selftest を確かめる」）で、金庫は GET を呼んで 403 を受ける。
       - 契約 §5 の 5 は KMS の 4xx を「条件に合わない」として終了するので、金庫は `OnFailure` で再起動を繰り返す。
       - 手動手順の切り分けは「KMS が 403: principalSet の digest が違う。IAM の反映待ち」なので、別の原因を探して点 2 の時間を使う。
       - 403 を消そうと `roles/cloudkms.viewer` を principalSet に足すと、§10 の (b) に反する。
       - §16 の「1 時間ごとに primary を確かめて包み直す」も、同じ GET で落ちる。
    2. primary の変更は結果整合で、反映までの間、Encrypt は前の primary を使うことがある。無効化した版も、通常 1 分（例外的に数時間）は使える（https://cloud.google.com/kms/docs/consistency ）。
       - §16 の順で数分のうちに本番の VM を作ると、最初の DEK が V1 で包まれ、`kek_version` が V1 になりうる。次の起動で「primary でない」で止まる。
       - live のデータの後の包み直しでも、包み直したはずの文書が前の版のままになる。
    3. 止まったときのログの固定文が「rotate the DEK」になっている。live のデータがあるときに案内どおり `tee_reset_dek.py --yes` を実行すると、封印済みのデータがすべて開けなくなる。正しい復旧は、前の版を primary に戻して起動し、包み直しを待つこと。
  - 根拠（公式）:
    - ロールの権限: https://docs.cloud.google.com/iam/docs/roles-permissions/cloudkms
    - GET に要る権限: https://cloud.google.com/kms/docs/reference/rest/v1/projects.locations.keyRings.cryptoKeys/get
    - Encrypt の応答の `name`（「意図した版で暗号化されたかを確かめるための項目」）: 同じ場所の `/encrypt`
    - Decrypt の応答の `usedPrimary`: 同じ場所の `/decrypt`
  - 提案:
    - primary は GET で読まない。どちらも `cryptoKeyEncrypterDecrypter` の範囲で判定できる。
      - 起動時に短い試しの Encrypt をして、応答の `name` を `kek_version` と比べる（復号の前に判定できる）。
      - または、Decrypt の応答の `usedPrimary` が真であることを求める。
      - 包み直しも、Encrypt の応答の `name` が新しい primary のときだけ `_tee/dek` を更新する。
    - 手順 D で版を回した後、`gcloud kms keys describe`・`versions list` で primary が V2、V1 が DISABLED になったことを確かめ、数分おいてから DEK を消す。金庫の起動のログに包んだ版の名前を出し、V2 であることを証跡にする。
    - 拒否のログの文を「前の版を primary に戻して再起動する。封印済みのデータがあるときは DEK を消さない」にする。

- [C-61] 種別: 設計 / 深刻度: medium
  - 指摘: §9 の 3 と契約 §16（C-57）の定期の再検証は、「失敗したらピンを外し、通るまで全要求を ConnectError にする」で、失敗の理由を区別しない（16 巡目の C-57 の案は 429・503 を除いていた）。
    - 金庫の `/v1/attestation` は、全体で 1 秒に 1 回まで（超えると 429）。
    - 公開の `GET /api/tee/attestation?nonce=` は、誰の要求でも 2 秒に 1 回まで金庫へ転送する。IP ごとの制限は書かれていない。
    - ピンを外した後に、いつ検証し直すかも書かれていない。

    X-67 で残した公開の転送と、C-57 で足した定期の再検証を組み合わせると、新しく起きる。
  - 破綻シナリオ:
    1. 匿名の利用者が、`/api/tee/attestation?nonce=<乱数>` を 2 秒ごとに呼ぶ。金庫の発行枠は、2 秒のうち 1 秒ふさがる。
    2. 10 分ごとの再検証の `/v1/attestation` は、約半分の確率で 429 になり、ピンが外れる。
       - single-flight で待っていた要求と、その間の全交渉の金庫の呼び出し（確かめ・手の登録・画面の読み出し）が、同時に失敗する。
       - launcher の一時的な 503 や、署名鍵の取得の失敗でも同じことが起きる。
       - 外しても安全は増えない。同じ証明書にピン留めしたままで、金庫が入れ替われば TLS が先に失敗するからだ。
    3. 「通るまで」を「次の 10 分の検証まで」と読む実装だと、1 回の 429 で、10 分間すべての金庫の呼び出しが止まる。
       - 手番の期限は 5 分なので、その間の live の交渉は、つながった後の最初の手の操作で `timeout` になり、双方に「なし」が出る。
       - 本物の候補者は、交渉ごとに評価 17 を 1 日 170 の予算から予約している。繰り返されると、その日に交渉を作れなくなる。
    4. 「要求のたびに（2 秒の下限で）検証し直す」と読む実装でも、攻撃の間は 10 分ごとに全交渉の呼び出しがまとめて落ちる。計画からのやり直しで、LLM の呼び出しも増える。
  - 提案:
    - 失敗を 2 つに分ける。
      - 方針の理由はピンを外す: `swname`・`debug`・`support_attributes`・`hwmodel`・`image_digest`・`override`・`project`・`service_account`・`signature`・`nonce`・`certificate`・`audience`・`issuer`。
      - 一時的な失敗（429・503・通信エラー・署名鍵の取得の失敗）は、ピンを保ったまま短い間隔（2 秒から指数で）で検証し直す。一定時間（例: 10 分）通らなければ外す。
    - `web` は、金庫の `/v1/attestation` を呼ぶすべての経路（初回・付け替え・定期・公開の転送）を、1 つの直列化と 1 秒以上の間隔に通し、内部の検証を公開の転送より優先する。VPC の中で金庫を呼べるのは `web`（と probe）だけなので、通常の運用では 429 が出なくなる。
    - ピンを外した後は「要求のたびに、2 秒の下限で検証し直す」と契約に書く。
    - DV に次の 2 つを足す。公開の転送を 2 秒ごとに流しながら定期の検証を繰り返しても、ピンが外れない。方針の理由ではピンが外れ、通るまで金庫を呼ばない。

### low のメモ（件数に数えない）

- L17-1（設計 / X-66 の補足）: §9 の「平文で残るメタデータ」が、実際より少ない。
  - 次の項目が平文で読める: `end_reason`（`agreed` なら合意した）、`status=awaiting_principal`（途中確認があった）、`paused`。
  - 金庫のログ（§3.8 は「判定結果」を書く）は、TEE の外の Cloud Logging に出る（`log_redirect`）。
  - DV-19 の「平文の項目の集合が §9 の列挙と一致」は、今の列挙では通らない。列挙に `to_move`・`paused`・`paused_at`・`version`・`request_id`・`end_reason`・`side`・`deleting` がない。
  - 対応: 列挙と説明文に足すか、`end_reason` を封印してログから判定結果を外す。
- L17-2（設計）: 手順 E の環境変数が、契約と合っていない。
  - `web` の更新コマンドに `VAULT_TEE=true`・`VAULT_SERVICE_ACCOUNT` がなく、契約にない `VAULT_AUDIENCE` を渡している。`VAULT_TEE` が既定の false のままだと、自己署名の証明書の検証で全呼び出しが落ちる。
  - probe の Job にも `VAULT_SERVICE_ACCOUNT` がない。契約 §10 は None なら SA を照合しないので、スパイクの点 3 の検証が本番の方針より弱くなる（probe はスパイクで使うので、「デプロイの段で確定」の対象外）。

### 問題なしとした観点（根拠と、最も危うい前提）

1. 失効（§16 の C-57）の形。
   - `allowed_digests` は `active` だけで、表示用に `revoked` も返す。失効は、表を直して `web` を再デプロイする。
   - 第三者の AC-23 は GitHub の表を使う。再デプロイを忘れると第三者の検証が `image_digest` で落ちるので、食い違いが外から見える。
   - イメージの失効（`swname=GCE`、`STABLE` が消える）は、定期の再検証で 10 分以内に止まる。
   - 最も危うい前提: 失効した版の KMS の束縛を外すことを、deploy_check の (b) でしか担保していないこと。再デプロイと deploy_check の間は、失効した版でも鍵を受け取れる。
2. X-69 の `last_check`。
   - §2.7・§3.1・§3.5（`check` で入れる）・§4.4（回答で P に置き換え）・DV-11（Q≠P）が、同じ定義で揃っている。
   - `ask_principal` と `propose` のガードの評価は、定義の列挙に入っていない。そのため `last_check` を変えず、矛盾しない。
   - L16-3 の代償（Q の評価し直しが消える）は、§4.4 と DV-11 に書かれている。
3. 定期の再検証そのものの安全性。
   - 再検証は、ピン留めした接続の上で新しい nonce を使う。証明書を差し替えた中継者は TLS で落ち、古いトークンの再送は nonce で落ちる。
   - 最も危うい前提は、失敗の扱い（C-61）。
4. §9 の 1 段目（動いているもの）と AC-23。
   - AC-23 の合格の列挙に `override` はないが、共有の検証関数（契約 §10 の 13）が見ているので、主張と検査は対応している。
   - 3 段目（ソースの由来）は、L0 では運営者の申告と明記されている。AC-23 も「証明するのは 1 段目と許可リストまで」と書いている。
