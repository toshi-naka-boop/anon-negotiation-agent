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

web の SA の ID トークンを自分で作れるようにします（点 4「金庫が呼び出し元を確かめる」の手元の試験に使います）。

```bash
gcloud iam service-accounts add-iam-policy-binding "$WEB_SA" --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountOpenIdTokenCreator
```

金庫の SA の ID トークンも作れるようにします（「web 以外の SA は 403」の試験に使います）。

```bash
gcloud iam service-accounts add-iam-policy-binding "$VAULT_SA" --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountOpenIdTokenCreator
```

金庫の SA になりすます権限を付けます（負の試験 3「オーナーでない主体は鍵を取れない」に使います。これも手順 F で外します）。

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
- 手順 C: debug イメージの VM `vault-tee` を作成（RUNNING、10.10.0.10、SEV）。以降の確認は進行中。
