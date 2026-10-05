# TEE スパイクの手動確認(点 1〜6)

- 対象: 設計書 `design/anon-negotiation-agent/design.md` §9 の確かめる 6 点と AC-23(10/3〜4 のスパイク)。
- 元: 研究報告 `design/anon-negotiation-agent/research/tee-spike.md` の「コマンドの手順」(0・A〜F)と「点ごとの合否の基準」。コードの名前・形は契約 `research/tee-spike-contract.md` §12〜§14。
- この文書がすること: 点ごとに、手順 A〜F のどの段で、どのコマンドを実行し、何が出れば合格か、何を貼るか、不合格のときの代替は何かを並べる。VM やビルドを作るコマンドそのもの(A〜F の本体)は繰り返さない。研究報告の手順を上から順に進め、各点の「どの手順の段で」に来たら、その点の確認を行う。
- 判定: 6 点すべての合格が、設計書 §9 の「TEE を実施する」の条件。縮退を使うかは、判定会でユーザーが決める(縮退を使うときは、画面と説明文にその旨を書く)。

## 共通の前提

**使い方**

- 研究報告の手順 0(変数)を、ターミナルごとに実行しておく。リポジトリの直下で実行する。
- 研究報告にあるコマンドは、そのまま写した。研究報告にないコマンドには、直前の説明文に「(追加)」と書き、理由を添えた。
- 出力は全部は貼らなくてよい。各点の「貼ってもらう出力」の範囲を、コマンドごとに貼る。エラーが出たら、そのブロックと出力をそのまま貼る。
- 貼らないもの: ID トークンの値、アクセストークン、`--print-token` の出力、KMS が返した平文。claim の値(`image_digest`・`hwmodel` など)は貼ってよい。
- 各点の時間の枠を 1.5 時間超えたら、その点の「不合格のときの代替」に切り替えて、判定会で扱う。

(追加)証跡を `tmp/tee_spike/` に保存する(`tmp/` は .gitignore 済み)。そのフォルダを作る。

```
mkdir -p tmp/tee_spike
```

(追加)手順 0 の変数が入っていることを確かめる(8 個とも値が出れば合格)。

```
echo "$PROJECT_ID $REGION $ZONE $PROJECT_NUMBER $WEB_SA $VAULT_SA $REPO $VAULT_AUDIENCE"
```

**IAP のトンネル**(点 1・3・4・6 が使う。研究報告の手順 C のコマンド)。別のターミナルで張り、使い終わるまで前面で動かしておく。

```
gcloud compute start-iap-tunnel vault-tee 8443 --local-host-port=localhost:8443 --zone="$ZONE"
```

## 点 1: 金庫のイメージを Confidential Space で動かす

### どの手順の段で

- B(Cloud Build でイメージを作り、digest を取る)→ C(debug イメージの VM で、起動とトークンの claim を見る)→ D(本番イメージの VM に切り替え、launcher のログを読む)→ F(停止と開始)。
- 時間の枠は 1.5 時間(10/3 昼)。
- 合格の基準(研究報告の表): 本番イメージの VM が起動し、Cloud Logging に launcher のログが出る。トークンに `swname=CONFIDENTIAL_SPACE`・`dbgstat=disabled-since-boot`・`hwmodel` が期待どおり・`image_digest` がビルドと一致。停止→開始と `OnFailure` の再起動が動く。縮退はない。

### 実行するコマンド

**B の前に**

(追加)ビルドの元のコミットと作業ツリーを一致させる。出力が空であること(空でなければ、先にコミットする。`_COMMIT` とビルドの中身が食い違うため)。

```
git status --short
```

**B(ビルドと digest)**

イメージをビルドする(研究報告 B)。

```
gcloud builds submit --region="$REGION" --config=cloudbuild.vault.yaml --substitutions=_IMAGE="${REPO}/vault:spike",_COMMIT="$(git rev-parse HEAD)" .
```

(権限のエラーで失敗したときだけ。研究報告 B)ビルドが使う Compute Engine の既定 SA にビルドの権限を付けて、上のビルドをやり直す。

```
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --role=roles/cloudbuild.builds.builder
```

digest を取得する(研究報告 B)。

```
export IMAGE_DIGEST=$(gcloud artifacts docker images describe "${REPO}/vault:spike" --format='value(image_summary.digest)')
```

digest を表示する(研究報告 B)。

```
echo "$IMAGE_DIGEST"
```

(追加)digest とコミットの対応を `deploy/vault-releases.json` に追記する(L0。web とスクリプトが許可する digest の表になる)。ビルドからこのコマンドまでの間にコミットしないこと(コミットが変わると、記録が実際の元とずれる)。追記したら、別のコミットにする。

```
uv run python scripts/tee_record_release.py --digest "$IMAGE_DIGEST" --commit "$(git rev-parse HEAD)"
```

**C(debug イメージの VM。VM は研究報告 C の作成コマンドで作る)**

VM のシリアル出力を見る(研究報告 C)。

```
gcloud compute instances get-serial-port-output vault-tee --zone="$ZONE"
```

launcher のログを Cloud Logging から読む(研究報告 C)。

```
gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\"" --freshness=1h --order=asc --limit=200 --format=json
```

(追加)トンネルを張った状態で、トークンの claim を確かめる(契約 §12 のスクリプト。debug イメージなので `--allow-debug` を付ける。claims の表と警告が出る)。

```
uv run python scripts/verify_attestation.py --direct https://localhost:8443 --project "$PROJECT_ID" --service-account "$VAULT_SA" --allow-debug
```

**D(本番イメージの VM。VM は研究報告 D の作成コマンドで作る)**

本番イメージの launcher のログを読む(研究報告 D)。`@sha256` の参照が通らなければ、`tee-image-reference` をタグ参照に戻す。

```
gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\"" --freshness=30m --order=asc --limit=200 --format=json
```

(追加)本番イメージのトークンの claim を確かめる(`--allow-debug` なし。本番の条件で全項目が通る)。

```
uv run python scripts/verify_attestation.py --direct https://localhost:8443 --project "$PROJECT_ID" --service-account "$VAULT_SA"
```

**F(停止と開始)**

VM を停止する(研究報告 F)。

```
gcloud compute instances stop vault-tee --zone="$ZONE"
```

VM を開始する(研究報告 F)。金庫が再起動し、DEK の復号と証明書の作り直しをやり直す。開始のあと、上の本番の claim の確認と launcher のログを、もう一度実行する。

```
gcloud compute instances start vault-tee --zone="$ZONE"
```

**OnFailure の再起動**(研究報告に手順がないので追加)。金庫は、鍵を受け取れないと非 0 で終了する(契約 §4・§5)。わざと鍵の権限を外して失敗させ、`tee-restart-policy=OnFailure` で再起動を繰り返すことを見る。

(追加)現在の digest の鍵の権限を外す(研究報告 F の `<OLD_DIGEST>` を `${IMAGE_DIGEST}` にしたもの)。

```
gcloud kms keys remove-iam-policy-binding vault-kek --location="$REGION" --keyring=vault-tee --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/attribute.image_digest/${IMAGE_DIGEST}" --role=roles/cloudkms.cryptoKeyEncrypterDecrypter
```

IAM の反映に数分かかるので、外してから 5 分ほど待つ。そのあと VM を停止し、開始する(上の F のコマンドを順に実行する)。数分待ってから、launcher のログを読む(上の D のコマンド)。失敗と再起動が繰り返し出ていること。

鍵の権限を戻す(研究報告 B)。

```
gcloud kms keys add-iam-policy-binding vault-kek --location="$REGION" --keyring=vault-tee --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/attribute.image_digest/${IMAGE_DIGEST}" --role=roles/cloudkms.cryptoKeyEncrypterDecrypter
```

これも反映に数分かかる。そのあとの launcher のログ(上の D のコマンド)に、手を触れずに再起動が成功して金庫が起動した行(`sealing self-test ok`)が出ること。

### 合格の出力

- `git status --short` が空。ビルドが `SUCCESS` で終わる。`IMAGE_DIGEST` が `sha256:` + 小文字の 16 進 64 桁。`tee_record_release.py` が `recorded:` と出力する。
- 本番イメージの VM で、launcher のログが Cloud Logging に出る。金庫のログに uvicorn の起動行がある(目安: `Uvicorn running on https://0.0.0.0:8443`。起動行の文言が違っても、次の `verify_attestation.py` が通れば起動している)。
- `verify_attestation.py` の終了コードが 0 で、claims の表が次の値になる。
  - `swname` が `CONFIDENTIAL_SPACE`
  - `dbgstat` が `disabled-since-boot`
  - `hwmodel` が `GCP_AMD_SEV`(TDX の VM なら `GCP_INTEL_TDX`)
  - `image_digest` が `echo "$IMAGE_DIGEST"` の値と同じ
- 停止→開始のあとも、同じ確認が通る(金庫の証明書は起動のたびに作り直されるが、スクリプトは毎回その場で証明書を控えて検証するので通る)。
- 鍵の権限を外して反映を待つと、金庫が失敗して再起動を繰り返す。権限を戻して反映を待つと、手を触れなくても再起動のあとに `sealing self-test ok` が出る。

### 貼ってもらう出力

- `git status --short` の出力(空なら「空」)。
- ビルドの最後の数行(`STATUS: SUCCESS` が分かる範囲)。
- `echo "$IMAGE_DIGEST"` の結果と、`tee_record_release.py` の出力。
- C の launcher のログと、debug イメージでの `verify_attestation.py` の出力(claims の表)。
- D の launcher のログ(起動・失敗・再起動を示す行を、前後 5 行つきで。全文でなくてよい)と、本番イメージでの `verify_attestation.py` の出力(claims の表の全体)。
- 停止→開始のあとの `verify_attestation.py` の出力。
- OnFailure の確認で、失敗と再起動が出ている launcher のログの抜粋と、復帰後の `sealing self-test ok` の行。

### 不合格のときの代替

- 不合格の目安: 本番イメージで 2 時間デバッグしても起動しない。
- 代替(研究報告 1-4): debug イメージで原因を切り分ける。公式の nginx の例(研究報告 [S2])で、土台の問題かを分ける。それでも駄目なら Cloud Run 版にする。
- 症状ごとの対処(研究報告の「危ない点」R2・R3・R5・R15・R16):
  - 本番イメージで SSH もログも出ない: `log_redirect=always` が効いているかを launcher のログで見る。debug のまま原因を切り分ける。
  - image の pull や Google API に出られない: 限定公開の Google アクセスを確かめ、足りなければ VM 用に Cloud NAT を足す(点 5 と同時に切り分ける)。
  - VM が作れない(在庫・クォータ): 別ゾーン(SEV は a・b・c)、別の機密技術、別リージョン。
  - ビルドが権限で失敗: 上の `roles/cloudbuild.builds.builder` のコマンド。Cloud Shell で docker build する手もある。
  - `@sha256` の参照が通らない: タグ参照にして、digest は KMS 側(点 2)で縛る。

## 点 2: 保存データの鍵を、検証済みのワークロードにだけ渡す

### どの手順の段で

- B の最後(KMS の鍵に digest の権限を付ける)→ C(テスト用の条件で鍵の解放を確かめる)→ D(本番の条件に更新 → 負の試験 1 → debug の VM を削除 → 古い `_tee/dek` を控える → KEK の新しい版を primary にして旧版を無効化 → DEK を消す → 本番イメージの VM を作る → 再起動 → 負の試験 C-56。研究報告 R22、契約 §16)→ 負の試験 2・3 と、文書が暗号文であることの確認。
- 時間の枠は 3 時間(10/3 午後)。STS の交換が 3 時間通らなければ不合格。
- 合格の基準(研究報告の表): 本番条件の VM の金庫が DEK を復号でき、封印した試験文書を Firestore に書いて読み戻せる。負の試験 3 つ(debug の VM・digest 違い・オーナーでない主体)が拒否され、オーナーの復号は Data Access ログに残る。手元から文書を読むと暗号文。

### 実行するコマンド

**B の最後(権限を付ける)**

この digest のワークロードだけが鍵を使えるようにする(研究報告 B)。

```
gcloud kms keys add-iam-policy-binding vault-kek --location="$REGION" --keyring=vault-tee --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/vault-tee-pool/attribute.image_digest/${IMAGE_DIGEST}" --role=roles/cloudkms.cryptoKeyEncrypterDecrypter
```

**C(テスト用の条件。debug の VM で鍵の解放を確かめる)**

launcher と金庫のログを読む(研究報告 C)。`sealing self-test ok` が出ていること。

```
gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\"" --freshness=1h --order=asc --limit=200 --format=json
```

(追加)金庫の自動試験(鍵の解放と封印。GCP には接続しない)。

```
uv run pytest tests/test_tee_key_release.py tests/test_tee_sealing.py -q
```

**D(本番の条件に更新し、鍵の版を回してから、本番イメージの VM を作る。負の試験 1 と C-56 を含む)**

研究報告 D のコマンドを、書いてある順に実行する(順序が大事。批評 X-70: 鍵の新しい版を primary にして古い版を無効化し、debug の間の DEK を消してから、本番の VM を初めて起動する。本番の最初の DEK が新しい版で包まれ、起動後に版を回さずに済む)。ここでは、各段で確かめることだけを書く。コマンドは研究報告 D のとおり。

1. プロバイダの条件を本番用に更新する(研究報告 D の 1 つ目)。
2. 負の試験 1: debug の VM がまだあるうちに、停止して開始する(研究報告 F のコマンド)。シリアル出力の末尾(研究報告 C の `get-serial-port-output`)に STS の 4xx のステータスが出て、金庫が終了していること(debug イメージは `STABLE` を持たず、本番条件で落ちる)。
3. debug の VM を削除する(破壊的な操作。名前を確かめてから実行する)。
3-2. KMS の権限を外し(研究報告 D の `remove-iam-policy-binding`)、**65 分以上待って**から付け直す(debug の間に出た連携トークン・attestation トークンの期限切れを待つ。批評 C-59)。
4. 古い `_tee/dek` を手元に控える(研究報告 D の `curl`。`tmp/tee_spike/old_dek.json` に `wrapped_dek`・`kek`・`kek_version` の項目が見えること。平文の DEK は含まれない)。
5. KEK の新しい版を作って primary にし、`versions list` で古い版の番号(`ENABLED` で primary でないもの)を確かめ、古い版を無効化する。
6. `uv run python scripts/tee_reset_dek.py --yes`(手元の ADC。無ければ先に `gcloud auth application-default login`)。接続先の表示が本物の Firestore とこのプロジェクトであること。`_tee/dek` と `_tee/selftest` の 2 件を消したと出ること。
7. 本番イメージの VM を作る。launcher のログに `sealing self-test ok` が出ること(これが合格の最初の項目)。`@sha256` の参照が通らなければ、`tee-image-reference` をタグ参照に戻して作り直す。
8. VM を再起動(`stop` → `start`。`reset` は vTPM のロックアウトのカウンタを増やすので使わない。2026-10-04 の実測)して、もう一度 `sealing self-test ok` が出ること(既存の暗号文 `_tee/selftest` が同じ DEK で開く。契約 §16)。
9. 負の試験(C-56): 控えた古い `_tee/dek` を書き戻し(研究報告 D の `PATCH`。HTTP 200)、VM を停止→開始すると、launcher のログに `non-primary key version` が出て金庫が終了すること(本番イメージの `OnFailure` は、失敗の 2 分後に VM の再起動を予約する形で現れる。`Reboot scheduled for …` の行。放っておくと約 3 分ごとに同じ失敗が繰り返される)。
10. 確かめたら、`tee_reset_dek.py --yes` と再起動で、新しい版の DEK を作り直す(`sealing self-test ok`)。

**負の試験 2(digest の違うイメージは、KMS に拒否される)**(研究報告に手順がないので追加)。コードを 1 行変える代わりに、`_COMMIT` を別の値でビルドする(ラベルが変わるので digest が変わる)。この digest には、鍵の権限を付けない。

(追加)別の digest のイメージを作る(研究報告 B のビルドの、タグと `_COMMIT` を変えたもの)。

```
gcloud builds submit --region="$REGION" --config=cloudbuild.vault.yaml --substitutions=_IMAGE="${REPO}/vault:negative",_COMMIT="0000000000000000000000000000000000000000" .
```

(追加)そのイメージで本番イメージの VM を別名で作る(研究報告 D の作成コマンドの、名前・固定 IP・参照・再起動の指定を変えたもの。失敗したら VM が止まる)。

```
gcloud compute instances create vault-tee-negative --zone="$ZONE" --machine-type=n2d-standard-2 --confidential-compute-type=SEV --maintenance-policy=MIGRATE --min-cpu-platform="AMD Milan" --shielded-secure-boot --image-project=confidential-space-images --image-family=confidential-space --boot-disk-size=20GB --network=vault-vpc --subnet=vault-subnet --no-address --tags=vault-tee --service-account="$VAULT_SA" --scopes=cloud-platform --metadata="^~^tee-image-reference=${REPO}/vault:negative~tee-container-log-redirect=cloud_logging"
```

数分待って、launcher のログを読む(上の D のコマンド)。別の VM のログも混ざるので、KMS のステータス 403 が書かれた行を探す。

(追加)試験が済んだら、この VM を削除する(研究報告 D の削除コマンドの、名前を変えたもの)。

```
gcloud compute instances delete vault-tee-negative --zone="$ZONE"
```

**負の試験 3(オーナーでない主体は復号できない。オーナーは復号できるが記録に残る)**(研究報告 2-8 の (b)。批評 C-58)。ダミーの暗号文を使う(本物の包まれた DEK は使わない。権限があっても平文が出ないようにするため)。

(追加)ダミーの暗号文のファイルを作る。

```
printf 'dummy' > tmp/tee_spike/dummy.bin
```

(追加)金庫の VM の SA を impersonate して復号を試みる(手順 A で付けた `serviceAccountTokenCreator` を使う)。`PERMISSION_DENIED`(403)が合格(VM の SA に復号権がない)。

```
gcloud kms decrypt --location="$REGION" --keyring=vault-tee --key=vault-kek --ciphertext-file=tmp/tee_spike/dummy.bin --plaintext-file=- --impersonate-service-account="$VAULT_SA"
```

(追加)自分(オーナー)で復号を試みる。基本ロールに復号権が含まれるので権限の検査は通り、ダミーの暗号文なので `INVALID_ARGUMENT` で終わる(これが期待どおり。構造的な限界の確認)。

```
gcloud kms decrypt --location="$REGION" --keyring=vault-tee --key=vault-kek --ciphertext-file=tmp/tee_spike/dummy.bin --plaintext-file=-
```

(追加)その試みが Cloud KMS の Data Access 監査ログに残っていることを確かめる(手順 A で有効にしたもの。自分のメールと `Decrypt` が出る)。

```
gcloud logging read 'protoPayload.serviceName="cloudkms.googleapis.com" AND protoPayload.methodName="Decrypt"' --freshness=15m --limit=5 --format="value(timestamp,protoPayload.authenticationInfo.principalEmail,protoPayload.status.code)"
```

**手元から文書を読むと暗号文**(研究報告に手順がないので追加)

(追加)Firestore の REST で、封印した試験文書を読む。

```
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" "https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/vault-db/documents/_tee/selftest"
```

### 合格の出力

- 本番条件の VM のログに `sealing self-test ok`(DEK を復号または新規作成でき、封印した試験文書を書いて読み戻せた)。
- 負の試験 1: debug の VM のシリアル出力に STS の 4xx のステータスが出て、金庫が終了する。
- KEK の入れ替え: `describe` の出力が、新しい版の番号(例 `.../cryptoKeyVersions/2`)で終わる。旧版で包んだ `_tee/dek` が残ったまま再起動すると、金庫は起動せず、launcher のログに `non-primary key version` が出て、`OnFailure` の再起動を繰り返す。旧版を無効化したあとは、KMS が旧版の包みの復号そのものを拒むので、古い `_tee/dek` を書き戻されても金庫は起動しない(そのときのログは、`non-primary key version` ではなく KMS のステータスになる)。
- DEK の作り直し: `tee_reset_dek.py` が `削除した: _tee/dek` と `削除した: _tee/selftest` を出力する(`もともと無かった` が出たら、`--project` が違うかもしれない)。VM の開始のあと、`sealing self-test ok` が出る(新しい DEK が、新しい primary の版で包まれて作られた)。
- 負の試験 2: 別 digest の VM の launcher のログに KMS の 403 が出て、金庫が終了する(VM が止まる)。
- 負の試験 3: impersonate した `gcloud kms decrypt` が `PERMISSION_DENIED`(403)で終わる。オーナー自身は権限の検査を通る(`INVALID_ARGUMENT`。これは期待どおりで、C-58 の構造的な限界)。Data Access ログに、その試みの主体(自分のメール)と `Decrypt` が出る。**impersonate で権限の検査を通ったら不合格**(VM の SA に復号権があることになる)。
- Firestore の REST の応答で、`fields.probe` が `bytesValue`(base64 の塊)で、読める文字列(`stringValue`)ではない。
- 縮退: 本番の条件が間に合わず、テスト用の条件(`swname` のみ)で鍵の解放が成立する。本番の条件は 10/5〜6 に回す。
- 不合格: 負の試験で鍵が漏れる(これは即、中止)。または 3 時間で STS の交換が通らない。または、旧版で包んだ `_tee/dek` で金庫が起動してしまう(本物のデータを入れない)。

### 貼ってもらう出力

- C の launcher のログ(`sealing self-test ok` を含む範囲)と、自動試験の最後の行(合格数)。
- 負の試験 1: シリアル出力の末尾(STS のステータスの行を含む範囲)。
- D の launcher のログ(`sealing self-test ok` を含む範囲)。
- KEK の入れ替え: `versions list` の出力(作る前と、無効化したあと)、`describe` の出力、旧版の包みで金庫が起動しなかったときの launcher のログ(`non-primary key version` の行を含む範囲)。
- `tee_reset_dek.py` の出力の全文と、そのあとの launcher のログ(`sealing self-test ok` の行)。
- 負の試験 2: 別 digest の VM の launcher のログ(KMS 403 の行を含む範囲)。
- 負の試験 3: impersonate での `gcloud kms decrypt` のエラーの全文、オーナーでの応答、Data Access ログの行(メールは自分のもの)。
- `curl` の応答の `fields` の部分(`probe` が `bytesValue` であること)。ID やトークンは含まれない。

### 不合格のときの代替

- 代替(研究報告 2-8): テスト用の条件(`swname` のみ)で debug の VM だけで確かめ、本番の条件は 10/5〜6 に回す。それでも鍵の解放が成立しなければ、TEE は設計書だけにする(spec の縮退順)。
- つまずいたときの切り分け(研究報告 2-7。一般的な挙動の推測):
  - STS が「属性条件で拒否」: 条件が false。debug は `support_attributes` が空で、本番条件の `STABLE` で落ちる。トークンの claim(`hwmodel`・`project_id`・`google_service_accounts`)を見る。
  - STS が「audience が合わない」: プロバイダの `--allowed-audiences` と、トークンの `aud`(`https://sts.googleapis.com`)。
  - KMS が 403: principalSet の digest がトークンの `image_digest` と違う。IAM の反映待ち(数分)。
  - KMS が 404: 鍵のパス(location・keyring・key)。
  - トークンのファイルが無い: launcher のログを見る。本番イメージで `log_redirect` が許されているか。
- 旧版で包んだ `_tee/dek` で金庫が起動してしまうとき: 本物のデータを入れない。金庫の鍵の解放の、primary の検査を直す(呼び出し側に報告する)。
- `tee_reset_dek.py` が認証や quota project のエラーで止まったら、表示される案内のコマンドを実行する(`gcloud auth application-default login`、`gcloud auth application-default set-quota-project "$PROJECT_ID"`。後者は一般的な対処の推測)。

## 点 3: web から金庫へ、ワークロード内で終端する TLS でつなぐ

### どの手順の段で

- 10/3 夕: C のトンネルで、手元の検証スクリプトを動かす。
- 10/4 午前: web 側の検証部品と transport の単体試験(異常系 6 つ)。
- 10/4 昼: E(Cloud Run の Job)で、ピン留めした接続の end-to-end。金庫を再起動して、もう一度確かめる(F)。
- 合格の基準(研究報告の表): 正常系(手元の検証スクリプトと Cloud Run の probe)でピン留めして通る。異常系 6 つ(nonce 違い・ハッシュ違い・署名破損・debug・digest 不許可・期限切れ)が自動試験で拒否される。金庫の再起動の後に再検証して復帰する。

### 実行するコマンド

**異常系 6 つ(自動試験)**

(追加)検証部品・ピン留めの transport・検証スクリプトの自動試験(契約 §14 の B のテスト。GCP には接続しない)。

```
uv run pytest tests/test_attestation_verification.py tests/test_attested_transport.py tests/test_verify_attestation.py -q
```

**正常系(手元)**

(追加)共通の前提のトンネルを張った状態で、金庫を検証する(契約 §12 のスクリプト。nonce を渡し、証明書のハッシュを控えて、トークンの署名・claim・digest を確かめる)。

```
uv run python scripts/verify_attestation.py --direct https://localhost:8443 --project "$PROJECT_ID" --service-account "$VAULT_SA"
```

**正常系(Cloud Run の Job)**

アプリのイメージを Cloud Build で作る(研究報告 E。直前に、点 1 の `tee_record_release.py` で digest を表に追記しておく。表はこのイメージに入る)。

```
gcloud builds submit --region="$REGION" --tag="${REPO}/app:spike" .
```

(追加)Direct VPC egress つきの Job を作る(研究報告 E のコマンドに、契約 §12 のとおり、金庫の SA を照合するための `VAULT_SERVICE_ACCOUNT` を足した)。

```
gcloud run jobs create tee-probe --region="$REGION" --image="${REPO}/app:spike" --service-account="$WEB_SA" --network=vault-vpc --subnet=run-egress-subnet --vpc-egress=private-ranges-only --command=python --args=scripts/tee_probe_client.py --set-env-vars="VAULT_BASE_URL=https://10.10.0.10:8443,VAULT_AUDIENCE=${VAULT_AUDIENCE},VAULT_SERVICE_ACCOUNT=${VAULT_SA}" --max-retries=0 --task-timeout=600
```

Job を実行して、終わるまで待つ(研究報告 E)。最初の接続に 1 分以上かかることがある。本番イメージの VM に対する確認なので、debug の VM に対して動かすときだけ、Job の環境変数に `TEE_PROBE_ALLOW_DEBUG=true` を足す(契約 §12)。

```
gcloud run jobs execute tee-probe --region="$REGION" --wait
```

Job のログを読む(研究報告 E)。

```
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="tee-probe"' --freshness=30m --order=asc --limit=200 --format="value(textPayload)"
```

**金庫の再起動のあと**

金庫の VM を停止し、開始する(研究報告 F。点 1 の F のコマンド)。起動して数分たってから、上の「手元」の検証スクリプトと、Job の実行(`gcloud run jobs execute tee-probe ...`)とログを、もう一度実行する。

### 合格の出力

- 異常系: 自動試験が全件通る(失敗 0)。
- 手元の検証スクリプトが終了コード 0 で終わり、claims の表が出る。
- Job の実行がエラーなく終わり、ログに次が出る。
  - 金庫の検証が通った結果と claims(`image_digest` が `echo "$IMAGE_DIGEST"` の値)。
  - ピン留めした接続の Bearer つきの呼び出しが 404、Bearer なしが 401(契約 §12。点 4 と共通)。
- 金庫を再起動したあとも、手元の検証スクリプトと Job が、もう一度通る(証明書が作り直されても、検証し直して通る)。
- 縮退: 証明書の固定(設定)で TLS はつなぐが、attestation との結び付けはスクリプトだけ。画面と文書に、弱い版だと明記する。
- 不合格: 金庫から launcher のトークンが取れない、または VM に届かない(点 5 と同時に切り分ける)。

### 貼ってもらう出力

- 自動試験の最後の行(合格数・失敗数)。
- 手元の検証スクリプトの出力(claims の表と終了コード。トークンの値は出さない)。
- Job の実行のコマンドの出力と、Job のログの全体(ID トークンの値は出ない。claim だけ)。
- 金庫の再起動のあとの、検証スクリプトと Job のログ。

### 不合格のときの代替

代替を弱い順に挙げる(研究報告 3-4)。

- (a) 証明書を設定で固定する(再起動ごとに手動で貼り替え。デモ中の可用性が悪い)。
- (b) 証明書を KMS で包んで保存し、再起動しても同じ証明書を使う(web は固定のピンを設定で持てる。実装が増える)。
- (c) pyOpenSSL で EKM(依存の追加の承認が要る)。
- (d) 結び付けなし(VPC 内 + ID トークンだけ)。「web が本物の金庫と話している」の確認ができないので、画面と文書で弱い版だと明記する。

## 点 4: 金庫が呼び出し元の ID トークンを自分で検証する

### どの手順の段で

- 10/3 夕: C のトンネルから、`curl` で 4 試験(コード無しで確かめられる)。
- 10/4 昼: E の Job で、Cloud Run のトークンの claim(`email`・`azp`)を確かめる。
- 形式不正・署名不正・期限切れの 401 は pytest で確かめる。
- 合格の基準(研究報告の表): 401 が 2 通り(トークンなし・audience 違い)、403 が 1 通り(別の SA)、web の SA は通る(手元の IAP 経由と、Cloud Run の probe の両方)。Cloud Run のトークンの claim(`email`・`azp`)を確認。

### 実行するコマンド

共通の前提のトンネルを張った状態で、別のターミナルで、次の 4 本の `curl` を実行する(どれも研究報告 C のコマンド)。

前提: 自分のユーザーに、web の SA と金庫の SA の `roles/iam.serviceAccountTokenCreator`(研究報告の手順 A)。`gcloud auth print-identity-token --impersonate-service-account` は先にアクセストークンを取るので、`roles/iam.serviceAccountOpenIdTokenCreator` だけでは `iam.serviceAccounts.getAccessToken` が拒否され、Bearer が空のまま 401 になる(2026-10-04 の実測)。

トークンなしの呼び出しが 401 になることを確かめる。

```
curl -sk -o /dev/null -w "%{http_code}\n" https://localhost:8443/v1/principals/0000000000000000/policy
```

自分のユーザーの ID トークン(audience が違う)が 401 になることを確かめる。

```
curl -sk -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $(gcloud auth print-identity-token)" https://localhost:8443/v1/principals/0000000000000000/policy
```

web の SA のトークンが認可を通ることを確かめる(存在しない依頼者なので 404 が返れば合格)。

```
curl -sk -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $(gcloud auth print-identity-token --impersonate-service-account="$WEB_SA" --audiences="$VAULT_AUDIENCE" --include-email)" https://localhost:8443/v1/principals/0000000000000000/policy
```

別の SA(金庫の SA)のトークンが 403 になることを確かめる。

```
curl -sk -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $(gcloud auth print-identity-token --impersonate-service-account="$VAULT_SA" --audiences="$VAULT_AUDIENCE" --include-email)" https://localhost:8443/v1/principals/0000000000000000/policy
```

Cloud Run の Job のログを読む(研究報告 E。点 3 の Job を実行したあと)。

```
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="tee-probe"' --freshness=30m --order=asc --limit=200 --format="value(textPayload)"
```

(追加)呼び出し元の検証の自動試験(契約 §14 の A のテスト。形式不正・署名不正・期限切れ・audience 違いの 401、別の SA の 403)。

```
uv run pytest tests/test_tee_caller_auth.py -q
```

### 合格の出力

- `curl` の 4 本が、上から順に `401`・`401`・`404`・`403`。
- Job のログに、Cloud Run のトークンの claim(`iss`・`aud`・`azp`・`email`・`email_verified`・`exp`)が出る。`email` が web の SA のメール、`email_verified` が真。トークンの値そのものは出ない。
- Job のログで、ピン留めした接続の Bearer つきの呼び出しが 404、Bearer なしが 401。
- 自動試験が全件通る。
- 縮退: トークンに `email` が無く、`azp`(SA の一意 ID)で照合する。
- 不合格: 署名の検証ができない。

### 貼ってもらう出力

- `curl` 4 本のステータスコード(4 行)。
- Job のログのうち、ID トークンの claim の行(トークンの値は貼らない)。
- 自動試験の最後の行(合格数・失敗数)。

### 不合格のときの代替

- `email` が無いとき(研究報告 R17): `azp` で照合する(縮退)。
- Cloud Run のメタデータサーバが URL 以外の audience を受け付けないとき(研究報告 R17): audience を URL 形式(`https://vault.<任意>`)にする。`config/params.toml` の `caller_audience` と、手順 0 の `VAULT_AUDIENCE` を同じ値に変える。
- 署名の検証ができないとき: 検証に使う証明書の取得(`https://www.googleapis.com/oauth2/v1/certs`)が VM から届いているかを、金庫のログで見る(点 5 と同時に切り分ける)。

## 点 5: VM を外部 IP なしで VPC の内側に置き、Cloud Run から VPC 経由でつなぐ

### どの手順の段で

- A(VPC・サブネット 2 つ・ファイアウォール・内部 IP の予約)→ C・D(VM を `--no-address` で作る)→ E(Cloud Run の Job が Direct VPC egress で金庫に届く)。
- 10/4 昼(2 時間の枠)。起動時の遅延(1 分以上)は、Direct VPC egress の既知の挙動。
- 合格の基準(研究報告の表): VM に外部 IP なし。image の pull と STS・KMS・Firestore・Logging が通る。Cloud Run の Job から金庫に 200。手元から直接は届かない。

### 実行するコマンド

VM に外部 IP が無いことを確かめる(研究報告 E)。

```
gcloud compute instances describe vault-tee --zone="$ZONE" --format="value(networkInterfaces[0].accessConfigs)"
```

本番イメージの launcher のログを読む(研究報告 D)。image の pull、STS・KMS・Firestore を通った `sealing self-test ok`、Logging への転送(このログが読めること自体)を見る。

```
gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/confidential-space-launcher\"" --freshness=30m --order=asc --limit=200 --format=json
```

Job を実行して、終わるまで待つ(研究報告 E。作り方は点 3)。

```
gcloud run jobs execute tee-probe --region="$REGION" --wait
```

Job のログを読む(研究報告 E)。

```
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="tee-probe"' --freshness=30m --order=asc --limit=200 --format="value(textPayload)"
```

(追加)手元から VM の内部 IP に直接つながらないことを確かめる(トンネルは localhost だけに張るので、これは通らない経路)。`000` が返れば合格。手元のネットワークや VPN が `10.10.0.0/28` を使っていないこと。

```
curl -sk -o /dev/null -w "%{http_code}\n" --connect-timeout 5 https://10.10.0.10:8443/v1/principals/0000000000000000/policy
```

### 合格の出力

- `accessConfigs` の出力が空。
- launcher のログに image の pull の失敗が無く、金庫のログに `sealing self-test ok` がある(Artifact Registry・STS・KMS・Firestore に届いた)。ログが Cloud Logging に出ている(Logging に届いた)。
- Job の実行がエラーなく終わり、ログで金庫に届いている(金庫の attestation が 200 で返って検証が通る。ID トークンありが 404、なしが 401)。
- 手元からの直接の接続が `000`(届かない)。
- 縮退: Cloud NAT かコネクタを足して通る(費用の増を許容する場合)。
- 不合格: どの代替でも届かない。

### 貼ってもらう出力

- `accessConfigs` のコマンドの出力(空なら「空」)。
- launcher のログのうち、pull・STS・KMS・Firestore・`sealing self-test ok` に関わる行。
- Job のログの全体(最初の接続にかかった時間が分かるように、時刻つきで)。
- 手元からの直接の接続の結果(`000` の行)。

### 不合格のときの代替

- 代替(研究報告 5-6・R3・R4): VM 用の Cloud NAT を足す(外部 IP の料金は $0.005/h。NAT 本体の料金は未確認)。Serverless VPC Access コネクタに替える(追加 $0.0215/h)。別ゾーン(a・c は SEV のみ)。
- Direct VPC egress の起動遅延(1 分以上)と接続切断が起きるとき: web の起動時に startup probe を付け、金庫への呼び出しは再試行する(`VaultClient` は通信エラーを `VaultUnavailableError` にして、レフェリーが待って再試行する作りなので、そのまま使える)。
- 金庫に届かないとき、ファイアウォールを見る: Cloud Run のサブネット `10.20.0.0/26` から `tcp:8443` を許可する規則と、VM の中の受信(Dockerfile の `EXPOSE`)の両方が要る。

## 点 6: 画面に attestation の内容と GitHub のコミットへのリンクを出す(AC-23)

### どの手順の段で

- B(digest とコミットの対応を表に記録。点 1)→ 10/4 午後: web の Cloud Run サービスに TEE モードをつなぐ(E の最後)→ `verify_attestation.py`(`--web` と `--direct`)と `/api/tee/attestation`。
- 画面(HTML)は範囲外。JSON まで。
- 合格の基準(研究報告の表): 画面と `verify_attestation.py` が、署名・claim・digest・コミットを確かめる。異常系(署名破損・nonce 違い・digest が表に無い・debug・期限切れ)で終了コード 1。IAP 経由で ID トークンなしが 401。

### 実行するコマンド

**web を TEE モードにする**

Direct VPC egress をつなぐ(研究報告 E の最後。web の Cloud Run サービスができた後)。

```
gcloud run services update web --region="$REGION" --network=vault-vpc --subnet=run-egress-subnet --vpc-egress=private-ranges-only --update-env-vars="VAULT_TEE=true,VAULT_BASE_URL=https://10.10.0.10:8443,VAULT_SERVICE_ACCOUNT=${VAULT_SA},GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
```

(追加)GitHub のリポジトリの URL を変数に入れる(コミットのリンクの土台。`<owner>/<repo>` を置き換える)。

```
export GITHUB_REPO_URL=https://github.com/<owner>/<repo>
```

(追加)web を TEE モードにする環境変数を足す(契約 §7。`VAULT_TEE`・金庫の SA・コミットのリンクの土台)。

```
gcloud run services update web --region="$REGION" --update-env-vars="VAULT_TEE=true,VAULT_SERVICE_ACCOUNT=${VAULT_SA},GITHUB_REPO_URL=${GITHUB_REPO_URL}"
```

(追加)web の URL を変数に入れる。

```
export WEB_URL=$(gcloud run services describe web --region="$REGION" --format='value(status.url)')
```

**画面の元になる JSON**

(追加)web が検証した結果を JSON で見る(契約 §8。長い `token` の項目は表示から外す)。`verified` が真、`release.commit` が表のコミット、`claims.image_digest` が `IMAGE_DIGEST` と同じ。

```
curl -s "${WEB_URL}/api/tee/attestation" | uv run python -c "import json, sys; d = json.load(sys.stdin); d.pop('token', None); print(json.dumps(d, indent=2, ensure_ascii=False))"
```

**AC-23(第三者の確かめ方)**

(追加)web 経由で、署名・claim・digest・コミットを確かめる(契約 §12)。

```
uv run python scripts/verify_attestation.py --web "$WEB_URL" --project "$PROJECT_ID" --service-account "$VAULT_SA"
```

(追加)共通の前提のトンネルを張った状態で、金庫に直接つないで確かめる。コミットが GitHub に実在することも確かめる(`--check-commit`。リポジトリが公開で、そのコミットが push 済みであること)。

```
uv run python scripts/verify_attestation.py --direct https://localhost:8443 --project "$PROJECT_ID" --service-account "$VAULT_SA" --check-commit
```

(追加)異常系の自動試験(署名破損・nonce 違い・digest が表に無い・debug・期限切れで終了コード 1。契約 §14 の B のテスト)。

```
uv run pytest tests/test_verify_attestation.py -q
```

**ID トークンなしの呼び出しは拒否される**

点 4 の最初の `curl`(トークンなしで 401)を、トンネル越しにもう一度実行する(研究報告 C)。

```
curl -sk -o /dev/null -w "%{http_code}\n" https://localhost:8443/v1/principals/0000000000000000/policy
```

### 合格の出力

- JSON が `"verified": true`。`release` に `commit`(40 桁)と `url`(`https://github.com/<owner>/<repo>/commit/<commit>`)と `built_at` がある。
- `verify_attestation.py` が `--web` と `--direct` の両方で終了コード 0 になり、claims の表(`image_digest`・`hwmodel`・`dbgstat` など)と、表から引いたコミットの SHA と URL が出る。`--check-commit` も通る。
- 異常系の自動試験が全件通る(失敗 0)。
- トークンなしの呼び出しが `401`。
- 縮退: コミットのリンクが手動の対応表だけ(L0)。再現ビルドと GitHub の証明つきビルド(L1)は 10/7 以降。
- 不合格: JWT の署名を検証できない。

### 貼ってもらう出力

- JSON の応答(`token` を除いたもの)。
- `verify_attestation.py` の出力(`--web`・`--direct` の両方。claims の表とコミットの行。`--print-token` は使わない)と、終了コード。
- 自動試験の最後の行(合格数・失敗数)。
- `curl` のステータスコード(1 行)。
- `deploy/vault-releases.json` の中身(digest・commit・built_at・status。公開情報)。

### 不合格のときの代替

- コミットの結び付け(研究報告 6-3): まず L0(運営者が `deploy/vault-releases.json` に記録した対応表。「運営者が、このコミットから作ったと記録した」まで)で出す。GitHub の証明つきビルド(L1)と再現ビルド(L2)は 10/7 以降の任意課題。
- `--check-commit` が通らないとき(リポジトリが非公開、またはコミットが未 push): `--check-commit` を付けずに確かめ、公開の範囲の判断(研究報告 R19: 台帳のプロジェクト ID と `design/` の公開)を先に行う。
- 画面(HTML)は 10/7 以降(契約 §8 では範囲外)。10/4 は JSON の返却までで確かめる(研究報告 R21)。画面なしを合格とするか縮退とするかは、判定会で決める。

## 判定会に持っていく表

6 点それぞれに、判定(合格・縮退・不合格)と、その根拠(貼った出力の場所)を書く。縮退と不合格には、使った代替と、画面と文書に書く注記を添える。

| 点 | 確かめること | 判定 | 根拠・代替 |
|---|---|---|---|
| 1 | 金庫のイメージを Confidential Space で動かす |  |  |
| 2 | 保存データの鍵を、検証済みのワークロードにだけ渡す |  |  |
| 3 | web から金庫へ、ワークロード内で終端する TLS でつなぐ |  |  |
| 4 | 金庫が呼び出し元の ID トークンを自分で検証する |  |  |
| 5 | VM を外部 IP なしで VPC の内側に置き、Cloud Run から VPC 経由でつなぐ |  |  |
| 6 | 画面に attestation の内容とコミットのリンクを出す(AC-23) |  |  |
