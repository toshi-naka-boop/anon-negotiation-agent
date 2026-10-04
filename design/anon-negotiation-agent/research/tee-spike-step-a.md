# TEE スパイク 手順 A の案内（準備。課金なし）

- 元の手順: `research/tee-spike.md` の「0. 変数」と「A. 準備」。コマンドはそこから写した（追加した 1 本には「（追加）」と書いた）。
- 書いた日: 2026-10-04。対象プロジェクト: `anon-nego-toshixa`（DV-15 の本物の Gemini の確認で使ったもの）。

## 1. 手順 A は何をするか

金庫（vault）を機密 VM（Confidential Space）で動かす前の、土台づくりです。VM もイメージもまだ作りません。作るのは次の 9 つです。

| 作るもの | 何のために | 費用 |
|---|---|---|
| API の有効化（13 個） | 以降の gcloud コマンドが動くように | 0 |
| サービスアカウント 2 つ（`vault-tee`・`web-run`）と権限 | 金庫の VM と web の Cloud Run が名乗る身元 | 0 |
| Artifact Registry のリポジトリ `vault` | 金庫とアプリのイメージの置き場 | 空なら 0 |
| Firestore のデータベース `vault-db` | 金庫の保存先（web の `(default)` とは別） | 空なら 0 |
| VPC `vault-vpc`・サブネット 2 つ・内部 IP・ファイアウォール 2 本 | 金庫を外部 IP なしで置き、Cloud Run からだけ届くようにする | 0（内部 IP は無料） |
| Cloud KMS のキーリング `vault-tee` と鍵 `vault-kek` | 金庫の保存データを封印する親鍵（KEK） | 鍵の版 1 つで月 $0.06 |
| Workload Identity Pool `vault-tee-pool` とプロバイダ `attestation-verifier` | 「検証に通った金庫のイメージにだけ鍵を渡す」仕組み。最初はテスト用の条件 | 0 |
| 自分のユーザーへの試験用の権限 3 つ | 点 4 の試験（ID トークンの検証）と負の試験のため。手順 F で外す | 0 |
| コンソールでの設定 2 つ（KMS の監査ログ、予算アラート） | オーナーが復号したときに記録が残るように。費用の上限の通知 | 0 |

所要時間はおよそ 20〜30 分です（API の有効化で 1〜2 分、Firestore の作成で数十秒待ちます）。

## 2. 始める前に

1. `gcloud` が入っていて、`gcloud auth login` が済んでいること。使うアカウントは、このプロジェクトのオーナーであること。
2. プロジェクトの課金が有効であること（手順 A では費用はほぼ出ませんが、API の有効化に課金アカウントの紐づけが要ります）。
3. 1 つのターミナルで最後まで続けること。変数は `export` で入れるので、別のタブに移ると消えます（移ったら A-0 からやり直す）。
4. リポジトリの直下（`~/dev/tenshokuagent`）で実行すること。
5. 最初のブロックで gcloud の既定のプロジェクトが `anon-nego-toshixa` に変わります。ほかの作業で別のプロジェクトを既定にしていたら、終わったあとに戻してください。
6. 貼ってはいけないもの: ID トークン・アクセストークンの値。手順 A では出ないはずです。アカウントのメールは貼って構いません（正本に書く値そのものです）。

## 3. 進め方

- ブロックを上から 1 つずつ実行します。チャットに貼ったブロックは Run ボタンで端末に送れます。
- 「already exists」「ALREADY_EXISTS」と出たら「すでにある」という意味なので、そのまま次へ進んでください（どのブロックで出たかは控えておく）。
- それ以外のエラーが出たら、そのブロックと出力をそのまま貼ってください。先には進まないでください。
- 終わったら、次の 5 つを送ってください。
  1. A-2 のクォータの表
  2. A-5 の `gcloud firestore databases list` の出力
  3. A-10 のオーナーの一覧
  4. エラーが出たブロックと出力（あれば）
  5. A-11 のコンソールの 2 つが済んだこと

「終わった」とだけ言ってもらえれば、端末の画面をこちらで読みます。

## A-0 変数（新しいターミナルを開くたびに）

固定の値を入れます。

```bash
export PROJECT_ID=anon-nego-toshixa REGION=asia-northeast1 ZONE=asia-northeast1-b
```

プロジェクト番号を取ります（Workload Identity Pool の指定に使います。数字が入れば合格）。

```bash
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
```

名前の類を入れます（web の SA、金庫の SA、イメージ置き場のパス、金庫を呼ぶときの audience）。

```bash
export WEB_SA="web-run@${PROJECT_ID}.iam.gserviceaccount.com" VAULT_SA="vault-tee@${PROJECT_ID}.iam.gserviceaccount.com" REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/vault" VAULT_AUDIENCE="https://vault.anon-nego.internal"
```

8 個とも値が出れば合格です。

```bash
echo "$PROJECT_ID $REGION $ZONE $PROJECT_NUMBER $WEB_SA $VAULT_SA $REPO $VAULT_AUDIENCE"
```

## A-1 既定のプロジェクトと API

gcloud の既定プロジェクトを設定します。

```bash
gcloud config set project "$PROJECT_ID"
```

必要な API をまとめて有効にします（1〜2 分かかります。`Operation ... finished successfully` で合格）。

```bash
gcloud services enable compute.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com cloudkms.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com confidentialcomputing.googleapis.com logging.googleapis.com firestore.googleapis.com iap.googleapis.com run.googleapis.com cloudasset.googleapis.com
```

何が有効になるか: Compute Engine（VM）、Artifact Registry（イメージ）、Cloud Build（イメージを作る）、Cloud KMS（鍵）、IAM と IAM Credentials と STS（身元と連携トークン）、Confidential Computing（attestation）、Logging、Firestore、IAP（検証中のトンネル）、Cloud Run、Cloud Asset（復号権の照合に使う Policy Analyzer）。

## A-2 クォータ（ここで詰まると日をまたぐので先に見る）

リージョンの CPU のクォータを出します。

```bash
gcloud compute regions describe "$REGION" --flatten="quotas[]" --format="table(quotas.metric,quotas.limit,quotas.usage)" | grep -E 'METRIC|N2D_CPUS|C3_CPUS|^CPUS'
```

見方: `N2D_CPUS` の LIMIT が 2 以上なら合格です（金庫の VM は `n2d-standard-2`。AMD SEV の機密 VM）。`C3_CPUS` は Intel TDX に切り替える場合にだけ 4 以上が要ります。`N2D_CPUS` が 0 なら、コンソールの「IAM と管理」→「割り当て」から引き上げを申請します（承認に数時間〜数日かかることがあるので、手順 A を今夜やる理由がこれです）。表はそのまま貼ってください。

## A-3 サービスアカウントと権限

金庫の VM が名乗るサービスアカウントを作ります。

```bash
gcloud iam service-accounts create vault-tee --display-name="vault TEE workload"
```

金庫の SA に、attestation トークンを発行してもらう権限を付けます（Confidential Space の中で「私はこのイメージです」と証明するために要ります）。

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${VAULT_SA}" --role=roles/confidentialcomputing.workloadUser
```

金庫の SA に、ログを書く権限を付けます。

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${VAULT_SA}" --role=roles/logging.logWriter
```

金庫の SA に、Firestore を `vault-db` だけで使える権限を付けます（IAM の条件つき。web の `(default)` には触れません）。

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${VAULT_SA}" --role=roles/datastore.user --condition="expression=resource.name==\"projects/${PROJECT_ID}/databases/vault-db\",title=vault-db-only"
```

web 用のサービスアカウントを作ります（すでにあれば「already exists」で飛ばします）。

```bash
gcloud iam service-accounts create web-run --display-name="web (Cloud Run)"
```

注意: 権限を付けるコマンドは、実行のたびにプロジェクトの IAM ポリシー全体が出力されます。長いですが正常です。貼らなくて構いません。

## A-4 イメージの置き場（Artifact Registry）

Docker 形式のリポジトリ `vault` を作ります。

```bash
gcloud artifacts repositories create vault --repository-format=docker --location="$REGION" --description="vault TEE and app images"
```

金庫の SA に、このリポジトリの読み取り権限を付けます（VM がイメージを取れるように）。

```bash
gcloud artifacts repositories add-iam-policy-binding vault --location="$REGION" --member="serviceAccount:${VAULT_SA}" --role=roles/artifactregistry.reader
```

## A-5 金庫のデータベース（Firestore `vault-db`）

データベースの一覧を出します（出力は貼ってください。`vault-db` があるかを見ます）。

```bash
gcloud firestore databases list --format="value(name)"
```

`vault-db` が無ければ作ります（あれば飛ばします。数十秒かかります）。

```bash
gcloud firestore databases create --database=vault-db --location="$REGION" --edition=standard --type=firestore-native
```

`--edition` が「unrecognized arguments」と出たら gcloud が古いので、`gcloud components update` で更新してから再実行してください。

## A-6 ネットワーク（金庫を外から見えない場所に置く）

専用の VPC を作ります。

```bash
gcloud compute networks create vault-vpc --subnet-mode=custom
```

金庫の VM 用のサブネットを作ります（限定公開の Google アクセスを有効にして、外部 IP なしでも KMS や Firestore に届くようにします）。

```bash
gcloud compute networks subnets create vault-subnet --network=vault-vpc --region="$REGION" --range=10.10.0.0/28 --enable-private-ip-google-access
```

Cloud Run が VPC に出るためのサブネットを作ります（Direct VPC egress は /26 以上が必要）。

```bash
gcloud compute networks subnets create run-egress-subnet --network=vault-vpc --region="$REGION" --range=10.20.0.0/26 --enable-private-ip-google-access
```

金庫の VM の内部 IP を固定で予約します（web が接続先として使います）。

```bash
gcloud compute addresses create vault-ip --region="$REGION" --subnet=vault-subnet --addresses=10.10.0.10
```

Cloud Run のサブネットから、金庫の 8443 番だけを許す規則を作ります。

```bash
gcloud compute firewall-rules create allow-run-to-vault --network=vault-vpc --direction=INGRESS --action=ALLOW --rules=tcp:8443 --source-ranges=10.20.0.0/26 --target-tags=vault-tee
```

検証中だけ、IAP のトンネル経由で 8443 と SSH を許す規則を作ります（手順 F で消します。`deploy_check` はこの規則が残っていると不合格にします）。

```bash
gcloud compute firewall-rules create allow-iap-to-vault --network=vault-vpc --direction=INGRESS --action=ALLOW --rules=tcp:8443,tcp:22 --source-ranges=35.235.240.0/20 --target-tags=vault-tee
```

## A-7 鍵（Cloud KMS）

キーリングを作ります。

```bash
gcloud kms keyrings create vault-tee --location="$REGION"
```

親鍵（KEK）を作ります。金庫はこの鍵で、保存データ用の鍵（DEK）を封印します。

```bash
gcloud kms keys create vault-kek --location="$REGION" --keyring=vault-tee --purpose=encryption
```

知っておくこと: KMS の鍵は消せません（版を「破棄予定」にできるだけ）。費用は有効な版 1 つにつき月 $0.06 です。手順 D で版を 1 つ増やすので、最終的に月 $0.12 ほどです。

## A-8 鍵を渡す相手を決める仕組み（Workload Identity）

プールを作ります。

```bash
gcloud iam workload-identity-pools create vault-tee-pool --location=global --display-name="vault TEE"
```

Confidential Space の attestation を受け付けるプロバイダを、テスト用の条件で作ります（1 行で長いですが、そのまま実行してください）。

```bash
gcloud iam workload-identity-pools providers create-oidc attestation-verifier --location=global --workload-identity-pool=vault-tee-pool --issuer-uri="https://confidentialcomputing.googleapis.com/" --allowed-audiences="https://sts.googleapis.com" --attribute-mapping='google.subject="gcpcs::"+assertion.submods.container.image_digest+"::"+assertion.submods.gce.project_number+"::"+assertion.submods.gce.instance_id,attribute.image_digest=assertion.submods.container.image_digest' --attribute-condition="assertion.swname == 'CONFIDENTIAL_SPACE' && assertion.submods.gce.project_id == '${PROJECT_ID}' && '${VAULT_SA}' in assertion.google_service_accounts"
```

何をしているか: 発行元は Google の attestation サービス、受け取り手は STS。主体の名前は「イメージのダイジェスト＋プロジェクト番号＋インスタンス ID」で作り、ダイジェストを属性にも写します（鍵の権限はこのダイジェストの単位で付けます）。条件は「Confidential Space で、このプロジェクトで、金庫の SA で動いていること」。この時点では debug イメージも通る緩い条件で、手順 D で本番イメージだけに締めます。

## A-9 試験用の権限（自分のユーザーに。手順 F で外す）

web の SA になりすます権限を付けます（点 4「金庫が呼び出し元を確かめる」の手元の試験に使います。gcloud のなりすましは先にアクセストークンを取るので、ID トークンだけの権限（OpenIdTokenCreator）では足りません。2026-10-04 の実行で分かったので直しました）。

```bash
gcloud iam service-accounts add-iam-policy-binding "$WEB_SA" --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountTokenCreator
```

金庫の SA になりすます権限を付けます（「web 以外の SA は 403」の試験と、負の試験 3「オーナーでない主体は鍵を取れない」に使います。手順 F で外します）。

```bash
gcloud iam service-accounts add-iam-policy-binding "$VAULT_SA" --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountTokenCreator
```

## A-10（追加）オーナーの一覧（P-20 の正本に書く値）

プロジェクトのオーナーの主体を一覧します（読み取りだけ。`user:` の後ろのメールを、手元の `tmp/tee_spike/expected-kms-principals.json` の `owners` に写します。公開の雛形には書きません（P-20 の判断 (b)）。この出力は貼ってください）。

```bash
gcloud projects get-iam-policy "$PROJECT_ID" --flatten="bindings[].members" --filter="bindings.role:roles/owner" --format="value(bindings.members)"
```

## A-11 コンソールで行う 2 つ

1. Cloud KMS の Data Access 監査ログを有効にする。コンソールの「IAM と管理」→「監査ログ」で「Cloud Key Management Service (KMS) API」を選び、「データ読み取り」と「データ書き込み」にチェックを入れて保存します。目的は、オーナーが鍵を使ったときに主体と時刻が残ること（批評 C-58）。コマンドで行わないのは、`set-iam-policy` がポリシー全体を上書きするためです。
2. 予算アラートを引き上げる。「お支払い」→「予算とアラート」で、閾値を 20,000 円（TDX なら 30,000 円。P-11）にし、50%・90%・100% で通知が来るようにします。

## 4. 終わったあと

- 手順 A の後に課金が始まるものはありません。ここで止めても費用はほぼ出ません。
- 次は手順 B（Cloud Build でイメージを作る。無料枠内）、手順 C（debug イメージで金庫を VM で動かす。ここから課金。VM は 1 時間およそ $0.125 ≈ 19 円。元の手順の「費用」の表）です。手順 C 以降は、点 1〜6 の「貼ってもらう出力」が `tests/manual/tee-spike.md` にあります。
- 片付けの手順は F にあります（VM の削除、IAP の規則の削除、試験用の権限の取り消し）。

## 5. 実施記録（2026-10-04。端末の出力から）

- A-0〜A-2: 変数は 8 個とも入った。API の有効化は成功。クォータは N2D_CPUS 16・C3_CPUS 24 で合格。
- A-3: `vault-tee`・`web-run` を作成。権限 3 つは付いた（条件つきの束縛で IAM ポリシーの版が 3 になった。想定どおり）。
- A-4: リポジトリ `vault` を作成し、読み取り権限を付けた。
- A-5: データベースの一覧は空だった（`(default)` も無い）。`vault-db` を作成（STANDARD・native・asia-northeast1・無料枠）。web 用の `(default)` は、手順 E かデプロイの前に作る（手順書に未記載。要追加）。
- A-6: `vault-vpc`・`vault-subnet`・`vault-ip`（10.10.0.10）・ファイアウォール 2 本を作成。`run-egress-subnet` のブロックは実行されていない（あとで実行する）。
- A-7: キーリングと鍵の作成は出力なしで終わった（成功時は無出力）。`gcloud kms keys list` で確かめる。
- A-8: プールとプロバイダを作成（テスト用の条件）。
- A-9: 自分のユーザーに試験用の権限 3 つを付けた（手順 F で外す）。
- A-10: オーナーは 1 件。個人の gmail で、git の作者（GitHub の noreply）とは別のアドレス。P-20 の「すでに公開されている」という前提が成り立たないため確認し、ユーザーの判断は (b)（2026-10-04）: 公開は雛形のまま、実値は手元の `tmp/tee_spike/expected-kms-principals.json`（gitignore 済み）に置き、`deploy_check` には `EXPECTED_KMS_PRINCIPALS_FILE` で渡す。
- A-11: 予算アラート（アラートのみ、20,000 円）と KMS の Data Access 監査ログは設定済み（ユーザーの申告）。
- 残りの 2 ブロック（`run-egress-subnet`、`kms keys list`）: 作成済み。鍵は版 1 が primary・ENABLED。
- 手順 B（2026-10-04 08:15 UTC）: Cloud Build SUCCESS（54 秒）。digest `sha256:1fc217043c8a1c06aac8829038f11197a7df2513c9408a3bf52af4a23a6c7d01`（元のコミット ca5791a）。鍵の権限を digest の principalSet に付けた。許可表 `deploy/vault-releases.json` に記録（別コミット）。
- 手順 C: debug イメージの VM `vault-tee` を作成（RUNNING、10.10.0.10、SEV）。`verify_attestation.py --allow-debug` は OK（image_digest がビルドと一致、hwmodel GCP_AMD_SEV、swname CONFIDENTIAL_SPACE、dbgstat enabled、project_id・SA が期待どおり、certificate_sha256 が eat_nonce と一致）。呼び出し元の検証（点 4）は 401・401・404・403 で合格。3 本目は最初 401 だったが、gcloud のなりすましに `roles/iam.serviceAccountTokenCreator` が要る（OpenIdTokenCreator では `iam.serviceAccounts.getAccessToken` が拒否される）ことが原因で、権限を足して数分の反映待ちの後に 404。手元の自動試験（鍵の解放・封印・呼び出し元の検証）は 248 件通過。
- 手順 C の launcher ログ（08:23:14Z）: `sealing self-test: created the probe` → `sealing self-test ok` → `Uvicorn running on https://0.0.0.0:8443`。テスト用の条件での鍵の解放（STS＋KMS）と封印の自己試験が通った。
- 手順 D 前半（2026-10-04）: プロバイダを本番の条件に更新（08:3x）。負の試験 1: debug VM を停止→開始すると、attestation トークン（dbgstat=enabled）は取れるが `key release: STS token exchange was refused (status 400)` → `startup failed at the step 'key release' (KeyReleaseError)` → launcher が exit_code=4 で終了（期待どおり）。debug VM を削除。旧 `_tee/dek` を `tmp/tee_spike/old_dek.json` に控えた。鍵の権限を外した（08:38Z ごろ。65 分の待ちは 09:44Z＝18:44 JST まで）。
- 待ち時間: アプリのイメージ `app:spike`（digest `sha256:be2bc9ac857524d29b6ef6c25df6073ef22b912fb49e304d372ced30d52d9f14`、Cloud Build 55 秒）。ADC ログイン済み（quota project = anon-nego-toshixa）。
- 手順 D 後半（12:5x〜13:1x UTC）: 権限を戻し、KEK の版 2 を作って primary、版 1 を無効化（`versions list` で 1 ENABLED→DISABLED、2 ENABLED）。`tee_reset_dek --yes` が `_tee/dek`・`_tee/selftest` を削除。本番イメージ（`vault@sha256:1fc217…`、`confidential-space`、cloud_logging、OnFailure）の VM を作成 → 12:53:19Z `sealing self-test: created the probe` → `sealing self-test ok` → `Application startup complete`（本番条件での鍵の解放＝D の最初の合格点）。reset 後 13:03:28Z に `created the probe` なしで `sealing self-test ok`（既存の暗号文が同じ DEK で開いた。契約 §16）。負の試験 C-56: 旧 `_tee/dek`（版 1）を PATCH（200）して reset → 13:13:24Z `DEK was wrapped by a non-primary key version; refusing to start. … (stored: …/1, primary: …/2)` → `startup failed at the step 'key release'` → launcher exiting（合格）。復帰は `tee_reset_dek` + reset（13:16:50Z に起動。self-test の行は読み出しが早すぎて未確認 → 再読で確認する）。
- 観察（要確認）: C-56 の失敗のあと、`tee-restart-policy=OnFailure` のはずだが、13:13:33Z の `TEE container launcher exiting` の後 3 分間に再起動の行が見えなかった（フィルタの外か、取り込みの遅れか、再起動していないか）。13:13:20Z 以降をフィルタなしで読んで確かめる。手順 F の OnFailure の試験（権限を外して停止→開始）でも見る。
- 手順 E（13:1x UTC）: Cloud Run Job `tee-probe`（Direct VPC egress、`run-egress-subnet`、web の SA）が成功（終了コード 0）。[1/4] ID トークンの claim: iss accounts.google.com、aud `https://vault.anon-nego.internal`、azp は SA の一意 ID、email は web の SA、email_verified True。[2/4] attestation の検証と証明書のピン留め OK: image_digest 一致、GCP_AMD_SEV、CONFIDENTIAL_SPACE、dbgstat disabled-since-boot、support_attributes [LATEST, STABLE, USABLE]、project・zone・instance 一致。[3/4] Bearer あり 404。[4/4] Bearer なし 401。VM の外部 IP なし（accessConfigs 空）。点 3・点 5・点 4 の Cloud Run 側が合格。
- web 用の Firestore `(default)` を作成（08:48:46Z。STANDARD・native・asia-northeast1。freeTier は vault-db が取っているので false）。
- 読み直し（13:13Z 以降、フィルタなし）: C-56 の失敗のあと launcher は `workload finished with a non-zero return code` → `TEE container launcher exiting` → `Reboot scheduled for 13:15:33 UTC`（本番イメージの OnFailure は VM の再起動として現れる）。復帰の起動（13:17:29Z）は `created a new DEK and stored it wrapped` → `templates seeded: written=0 skipped=6` → `created the probe` → `sealing self-test ok` → Uvicorn 起動。Launch Spec は `RestartPolicy:OnFailure Hardened:true LogRedirect:cloud_logging`、イメージの参照は `vault@sha256:1fc217…`。
- **要対応**: `Failed orderly startup. Avoid using instance reset. Instead, use instance stop/start. DA lockout counter incremented: LockoutCounter: 8 / MaxAuthFail: 32`。`reset` を 8 回使った。以後は停止→開始だけを使う（手順書 D・G と手動確認を直した）。カウンタは 2 時間に 1 つ回復（RecoveryTime 7200）。
- 小さな積み残し: launcher が `tee.launch_policy.monitoring_memory_allow` は非推奨で `tee.launch_policy.hardened_monitoring` / `debug_monitoring` を使えと警告（Dockerfile.vault のラベル。直すと digest が変わるので、次のイメージの作り直しのときに）。
- 負の試験 3（13:21Z）: 金庫の SA を impersonate した `kms decrypt` は `PERMISSION_DENIED`（`cloudkms.cryptoKeyVersions.useToDecrypt` なし。合格）。オーナー自身は `INVALID_ARGUMENT: Decryption failed: the ciphertext is invalid`（権限の検査は通る＝C-58 の構造的な限界、期待どおり）。Data Access 監査ログに 2 件: 13:21:24Z 金庫の SA（status 7）、13:21:32Z オーナー（status 3）。
- 手元から `_tee/selftest` を REST で読むと `probe` は `bytesValue`（暗号文）、`probe_sha256` は検査用のハッシュ。合格。
- 13:2x UTC: VM を停止（夜間。課金はディスクだけ）。

## 6. 2 日目（2026-10-04 深夜。ユーザーの指示で Claude が端末にコマンドを送って進めた。方式 B）

- 組織の有無: `gcloud projects get-ancestors` はプロジェクト 1 行だけを返した（組織もフォルダも無い）。P-13 の答え「組織の配下」は実際と違う。拒否ポリシー（手順 G）が作れるかは実測で確かめる。`deploy_check` は ORG_ID 無し＝`--project` の範囲で動く（祖先に組織があれば NG にする作りなので、そのままで整合する）。
- 負の試験 2（13:37Z）: `_COMMIT` だけ変えたイメージ `vault:negative`（digest `sha256:c0f169d0…`。spike と違う）で本番イメージの VM `vault-tee-negative` を作成。ログ: `exchanged the claims token for an access token`（STS は通る。プロバイダの条件は digest を見ない）→ `key release: KMS encrypt (primary probe) was refused (status 403)` → `startup failed at the step 'key release'` → launcher exiting。digest の結び付きが KMS の側で効いている。合格。VM とイメージは削除。
- 注意: `gcloud logging read --freshness` は `--order=asc` と組み合わせると古い行から返した（窓が効かない）。時刻は `timestamp>="…"` をフィルタに書く。
