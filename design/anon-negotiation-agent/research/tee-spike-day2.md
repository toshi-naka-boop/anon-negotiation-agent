# TEE スパイク 2 日目の手順（残りの試験と片付け）

- 書いた日: 2026-10-04 夜。前日の結果は `research/tee-spike-step-a.md` §5。元の手順は `research/tee-spike.md`（B・D・F・G）と `tests/manual/tee-spike.md`（点 1・2）。
- いまの状態: 本番イメージの金庫 VM `vault-tee` は停止中（鍵は版 2 で包まれた DEK。暗号文は自己試験の文書だけ）。鍵の権限は spike の digest に付いている。試験用の権限（なりすまし 4 件）と IAP の規則は残っている。
- **再起動は必ず停止→開始**（`reset` は仮想 TPM のロックアウトのカウンタを増やす。いま 8/32）。
- 所要の目安: 1〜2 と 4 で 25 分。3（任意）を足すと 45 分。5 は組織の権限しだい。

## 進め方

- 上から順に、1 ブロックずつ。「終わった」と言ってもらえれば端末を読んで判定する。
- 待ちはブロックの中に `sleep` で入れてある。待ちの間にほかのブロックを実行しない。
- 貼ってはいけないもの: トークンの値。下のコマンドはどれも出さない。

## 0. 変数（新しいターミナルで始めるとき）

```bash
export PROJECT_ID=anon-nego-toshixa REGION=asia-northeast1 ZONE=asia-northeast1-b
```

```bash
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
```

```bash
export WEB_SA="web-run@${PROJECT_ID}.iam.gserviceaccount.com" VAULT_SA="vault-tee@${PROJECT_ID}.iam.gserviceaccount.com" REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/vault" VAULT_AUDIENCE="https://vault.anon-nego.internal"
```

spike のイメージの digest を入れ直す（`sha256:1fc217…` が出れば合格）。

```bash
export IMAGE_DIGEST=$(gcloud artifacts docker images describe "${REPO}/vault:spike" --format='value(image_summary.digest)') && echo "$IMAGE_DIGEST"
```

## 1. 負の試験 2: digest の違うイメージは鍵を取れない（約 10 分。本番の VM は停止のまま行う）

コードを変える代わりに、ビルドのラベル（`_COMMIT`）だけを変えて、中身は同じで digest だけ違うイメージを作る。この digest には鍵の権限を付けない。本番の VM が止まっている間に行うと、ログがこの VM のものだけになる。

別 digest のイメージを作る（1 分）。

```bash
gcloud builds submit --region="$REGION" --config=cloudbuild.vault.yaml --substitutions=_IMAGE="${REPO}/vault:negative",_COMMIT="0000000000000000000000000000000000000000" .
```

digest を取り、spike と違うことを確かめる（2 行が違えば合格）。

```bash
export NEGATIVE_DIGEST=$(gcloud artifacts docker images describe "${REPO}/vault:negative" --format='value(image_summary.digest)') && echo "$IMAGE_DIGEST" && echo "$NEGATIVE_DIGEST"
```

そのイメージで本番イメージの VM を別名で作り、3 分待ってログを読む。期待: `exchanged the claims token for an access token`（STS は通る。プロバイダの条件は digest を見ないため）→ `key release: … was refused (status 403)`（KMS が digest の principalSet で拒む）→ `startup failed at the step 'key release'`。`sealing self-test ok` が出たら不合格（即、中止して報告）。

```bash
gcloud compute instances create vault-tee-negative --zone="$ZONE" --machine-type=n2d-standard-2 --confidential-compute-type=SEV --maintenance-policy=MIGRATE --min-cpu-platform="AMD Milan" --shielded-secure-boot --image-project=confidential-space-images --image-family=confidential-space --boot-disk-size=20GB --network=vault-vpc --subnet=vault-subnet --no-address --tags=vault-tee --service-account="$VAULT_SA" --scopes=cloud-platform --metadata="^~^tee-image-reference=${REPO}/vault:negative~tee-container-log-redirect=cloud_logging" && sleep 180 && gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\" AND (jsonPayload.MESSAGE:\"key release\" OR jsonPayload.MESSAGE:\"sealing\" OR jsonPayload.MESSAGE:\"rror\" OR jsonPayload.MESSAGE:\"exit\")" --freshness=6m --order=asc --limit=40 --format="value(timestamp,jsonPayload.MESSAGE)"
```

試験用の VM を消す（確認が出たら `y`）。

```bash
gcloud compute instances delete vault-tee-negative --zone="$ZONE"
```

（任意）試験用のイメージも消す。

```bash
gcloud artifacts docker images delete "${REPO}/vault:negative" --delete-tags --quiet
```

## 2. 本番の金庫を開始する（停止→開始の試験と、本番条件の attestation。約 5 分）

開始して 2 分待ち、ログを読む。期待: `unwrapped the stored DEK` → `sealing self-test ok`（`created the probe` なし。停止→開始で鍵と暗号文が保たれた）。

```bash
gcloud compute instances start vault-tee --zone="$ZONE" && sleep 120 && gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\" AND (jsonPayload.MESSAGE:\"key release\" OR jsonPayload.MESSAGE:\"sealing\" OR jsonPayload.MESSAGE:\"rror\" OR jsonPayload.MESSAGE:\"Uvicorn\")" --freshness=4m --order=asc --limit=40 --format="value(timestamp,jsonPayload.MESSAGE)"
```

トンネルを裏で張る。

```bash
gcloud compute start-iap-tunnel vault-tee 8443 --local-host-port=localhost:8443 --zone="$ZONE" > tmp/tee_spike/tunnel.log 2>&1 &
```

本番条件で attestation を確かめる（`--allow-debug` なし。`OK` と claims の表。`dbgstat` が `disabled-since-boot`、`support_attributes` に `STABLE`、`image_digest` が spike の digest）。これが点 1 の「本番イメージの claim」の証跡。

```bash
sleep 10 && uv run python scripts/verify_attestation.py --direct https://localhost:8443 --project "$PROJECT_ID" --service-account "$VAULT_SA"
```

トンネルを閉じる。

```bash
pkill -f "start-iap-tunnel vault-tee"
```

## 3. （任意）OnFailure の往復: 鍵が取れない間は失敗を繰り返し、権限が戻れば手を触れずに復帰する（約 20 分）

前日に、失敗の 2 分後に `Reboot scheduled` で VM が再起動することまでは見えている。ここでは「権限を戻すと自動で復帰する」ところまで見る。時間がなければ飛ばしてよい（判定会では「再起動の予約まで確認」と書く）。

鍵の権限を外し、IAM の反映を 5 分待ってから停止→開始し、3 分待ってログを読む。期待: `was refused (status 403)` → `startup failed` → `Reboot scheduled`。

```bash
gcloud kms keys remove-iam-policy-binding vault-kek --location="$REGION" --keyring=vault-tee --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/attribute.image_digest/${IMAGE_DIGEST}" --role=roles/cloudkms.cryptoKeyEncrypterDecrypter && sleep 300 && gcloud compute instances stop vault-tee --zone="$ZONE" && gcloud compute instances start vault-tee --zone="$ZONE" && sleep 180 && gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\" AND (jsonPayload.MESSAGE:\"refused\" OR jsonPayload.MESSAGE:\"startup failed\" OR jsonPayload.MESSAGE:\"Reboot\" OR jsonPayload.MESSAGE:\"sealing\")" --freshness=5m --order=asc --limit=40 --format="value(timestamp,jsonPayload.MESSAGE)"
```

4 分待って、2 回目の失敗が出ていること（繰り返しの確認）。

```bash
sleep 240 && gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\" AND (jsonPayload.MESSAGE:\"refused\" OR jsonPayload.MESSAGE:\"startup failed\" OR jsonPayload.MESSAGE:\"Reboot\" OR jsonPayload.MESSAGE:\"sealing\")" --freshness=10m --order=asc --limit=40 --format="value(timestamp,jsonPayload.MESSAGE)"
```

権限を戻し、8 分待ってログを読む。期待: 手を触れずに、どこかの再起動で `unwrapped the stored DEK` → `sealing self-test ok`。出ていなければ、もう 4 分待って読み直す（IAM の反映と再起動の周期が合わないことがある）。それでも出なければ停止→開始。

```bash
gcloud kms keys add-iam-policy-binding vault-kek --location="$REGION" --keyring=vault-tee --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/attribute.image_digest/${IMAGE_DIGEST}" --role=roles/cloudkms.cryptoKeyEncrypterDecrypter && sleep 480 && gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\" AND (jsonPayload.MESSAGE:\"refused\" OR jsonPayload.MESSAGE:\"startup failed\" OR jsonPayload.MESSAGE:\"Reboot\" OR jsonPayload.MESSAGE:\"sealing\" OR jsonPayload.MESSAGE:\"unwrapped\")" --freshness=12m --order=asc --limit=60 --format="value(timestamp,jsonPayload.MESSAGE)"
```

## 4. 片付け（手順 F。約 3 分）

前日に自分のユーザーへ付けた試験用の権限 4 件を外す。

```bash
gcloud iam service-accounts remove-iam-policy-binding "$WEB_SA" --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountTokenCreator
```

```bash
gcloud iam service-accounts remove-iam-policy-binding "$VAULT_SA" --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountTokenCreator
```

```bash
gcloud iam service-accounts remove-iam-policy-binding "$WEB_SA" --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountOpenIdTokenCreator
```

```bash
gcloud iam service-accounts remove-iam-policy-binding "$VAULT_SA" --member="user:$(gcloud config get-value account)" --role=roles/iam.serviceAccountOpenIdTokenCreator
```

IAP の規則を消す（手元から金庫に届く経路を閉じる。以後、金庫に届くのは Cloud Run の経路だけ。`deploy_check` の (f) はこの規則が無いことを確かめる）。

```bash
gcloud compute firewall-rules delete allow-iap-to-vault --quiet
```

鍵の権限の一覧を見て、spike の digest の 1 件だけであること（貼ってください）。

```bash
gcloud kms keys get-iam-policy vault-kek --location="$REGION" --keyring=vault-tee
```

## 5. 拒否ポリシー（手順 G。組織の権限が要る。約 10 分＋反映待ち）

目的: オーナーを含むすべての主体から KEK の暗号化・復号の権限を拒否し、例外を金庫のプールのワークロードだけにする（P-13 の答え「組織の配下」なので使える。これが入ると、前日の「オーナーは `INVALID_ARGUMENT`」が `PERMISSION_DENIED` に変わる）。

組織 ID を調べる（`TYPE` が `organization` の行の `ID`）。

```bash
gcloud projects get-ancestors "$PROJECT_ID"
```

```bash
export ORG_ID=<組織 ID>
```

自分に拒否ポリシーの管理者を付けてみる。`PERMISSION_DENIED` なら組織の管理者に頼む必要があるので、ここで止めて報告する（設計の既定「監査ログによる記録」のまま進める）。

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="user:$(gcloud config get-value account)" --role=roles/iam.denyAdmin
```

本文を書く。

```bash
cat > tmp/tee_spike/deny-kms.json <<EOF
{
  "displayName": "vault KEK: only the TEE workload pool may use the key",
  "rules": [
    {
      "denyRule": {
        "deniedPrincipals": ["principalSet://goog/public:all"],
        "exceptionPrincipals": ["principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/*"],
        "deniedPermissions": [
          "cloudkms.googleapis.com/cryptoKeyVersions.useToDecrypt",
          "cloudkms.googleapis.com/cryptoKeyVersions.useToEncrypt"
        ]
      }
    }
  ]
}
EOF
```

付ける。

```bash
gcloud iam policies create vault-kek-deny --attachment-point="cloudresourcemanager.googleapis.com/projects/${PROJECT_ID}" --kind=denypolicies --policy-file=tmp/tee_spike/deny-kms.json
```

本文を確かめる（貼ってください。`deploy_check` の (i) の期待値になる）。

```bash
gcloud iam policies get vault-kek-deny --attachment-point="cloudresourcemanager.googleapis.com/projects/${PROJECT_ID}" --kind=denypolicies --format=json
```

3 分待って、オーナーの復号が今度は `PERMISSION_DENIED` になること（前日は `INVALID_ARGUMENT` だった）。

```bash
sleep 180 && gcloud kms decrypt --location="$REGION" --keyring=vault-tee --key=vault-kek --ciphertext-file=tmp/tee_spike/dummy.bin --plaintext-file=-
```

金庫（プールのワークロード）はこれまでどおり起動できること。停止→開始して 2 分待ち、`sealing self-test ok`。

```bash
gcloud compute instances stop vault-tee --zone="$ZONE" && gcloud compute instances start vault-tee --zone="$ZONE" && sleep 120 && gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\" AND (jsonPayload.MESSAGE:\"key release\" OR jsonPayload.MESSAGE:\"sealing\" OR jsonPayload.MESSAGE:\"rror\")" --freshness=4m --order=asc --limit=40 --format="value(timestamp,jsonPayload.MESSAGE)"
```

## 6. 一日の終わり

VM を止める（審査期間に入ったら止めない）。

```bash
gcloud compute instances stop vault-tee --zone="$ZONE"
```

## 7. そのあと（スパイクの外。Claude が手順を用意する）

- web と agents の Cloud Run へのデプロイ（Secret Manager のクッキー署名鍵、環境変数、`agents` の起動元を web の SA だけに、web に Direct VPC egress と `VAULT_TEE=true`）。デプロイのコマンドはまだ手順書に無いので、Claude が `research/deploy-runbook.md` として用意する。
- 点 6: 画面に attestation の内容と GitHub のコミットへのリンクが出ること。`uv run python scripts/verify_attestation.py --web <web の URL>`。
- `scripts/deploy_check.sh`（`EXPECTED_KMS_PRINCIPALS_FILE=tmp/tee_spike/expected-kms-principals.json ORG_ID=<組織 ID>` を付けて）。
- 設計書 v22（台帳 I-35 の反映: P-20 (b)、`(default)` の作成、stop/start、OnFailure の形、ラベルの非推奨）。
