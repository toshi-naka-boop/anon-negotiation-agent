# デプロイ手順（web・agents を Cloud Run へ。金庫は TEE 版）

- 書いた日: 2026-10-04 深夜（TEE スパイクの A〜F が済んだあと）。設計書 §10「本番の必須設定」と `scripts/deploy_check.sh` の項目に合わせてある。
- 前提: 手順 A〜E が済んでいる（金庫の VM `vault-tee` が本番イメージで動き、`vault-vpc` と `run-egress-subnet`、Artifact Registry の `vault` リポジトリ、Firestore の `vault-db` と `(default)` がある）。
- 実行者: gcloud はユーザーのアカウントで動く。権限を付ける（`add-iam-policy-binding`）ブロックは、Claude の自動実行では安全確認に止まるので、ユーザーが Run を押す。それ以外は Claude が端末に送ってよい（方式 B）。
- 費用の目安: web は min 1・CPU 常時割り当てなので、1 vCPU・1 GiB で 1 時間およそ $0.07（≈ 10 円）、1 日およそ 250 円。agents は min 0。

## 0. 変数（新しいターミナルごと）

```bash
export PROJECT_ID=anon-nego-toshixa REGION=asia-northeast1 ZONE=asia-northeast1-b PROJECT_NUMBER=341888860511
```

```bash
export WEB_SA="web-run@${PROJECT_ID}.iam.gserviceaccount.com" AGENTS_SA="agents-run@${PROJECT_ID}.iam.gserviceaccount.com" VAULT_SA="vault-tee@${PROJECT_ID}.iam.gserviceaccount.com" REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/vault"
```

Cloud Run の URL は決定的（サービス名・プロジェクト番号・リージョンから決まる）なので、デプロイの前から分かる。

```bash
export AGENTS_URL="https://agents-${PROJECT_NUMBER}.${REGION}.run.app" WEB_URL="https://web-${PROJECT_NUMBER}.${REGION}.run.app"
```

## 1. サービスアカウントと権限（ユーザーが実行）

agents 用のサービスアカウントを作る（web・金庫とは分ける。§10）。

```bash
gcloud iam service-accounts create agents-run --display-name="agents (Cloud Run)"
```

agents の SA に Vertex AI（Gemini）を呼ぶ権限を付ける。

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${AGENTS_SA}" --role=roles/aiplatform.user
```

web の SA にも Vertex AI の権限を付ける（面談エージェントは web の中で動く。§5）。

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${WEB_SA}" --role=roles/aiplatform.user
```

web の SA に、Firestore を `(default)` だけで使える権限を付ける（金庫の `vault-db` には触れない。条件つき）。

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${WEB_SA}" --role=roles/datastore.user --condition="expression=resource.name==\"projects/${PROJECT_ID}/databases/(default)\",title=default-db-only"
```

## 2. セッションクッキーの署名鍵（Secret Manager）

鍵を作って Secret Manager に入れる（値は画面に出さない。32 バイトの乱数の base64url）。

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" | gcloud secrets create session-signing-key --data-file=- --replication-policy=automatic
```

web の SA に、この秘密の読み取りだけを許す（ユーザーが実行）。

```bash
gcloud secrets add-iam-policy-binding session-signing-key --member="serviceAccount:${WEB_SA}" --role=roles/secretmanager.secretAccessor
```

## 3. agents の URL を設定に書き、アプリのイメージを作る

`config/params.toml` の `[agents] public_base_url` を `AGENTS_URL` の値（`https://agents-341888860511.asia-northeast1.run.app`）にしてコミットする（Claude が行う。web は設定からこの URL を読み、agents はこの URL を自分のカードに書く）。

アプリのイメージを作る（タグはコミットの短い SHA。作業ツリーが空であること）。

```bash
gcloud builds submit --region="$REGION" --tag="${REPO}/app:$(git rev-parse --short HEAD)" .
```

## 4. agents をデプロイする（IAM で守る。公開しない）

```bash
gcloud run deploy agents --region="$REGION" --image="${REPO}/app:$(git rev-parse --short HEAD)" --service-account="$AGENTS_SA" --no-allow-unauthenticated --command=uvicorn --args="agents.app:create_app,--factory,--host,0.0.0.0,--port,8080" --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false" --min-instances=0 --max-instances=2 --concurrency=20 --memory=1Gi --cpu=1 --timeout=300
```

起動元を web の SA だけにする（ユーザーが実行。`deploy_check` の `iam-agents`）。

```bash
gcloud run services add-iam-policy-binding agents --region="$REGION" --member="serviceAccount:${WEB_SA}" --role=roles/run.invoker
```

URL が設定の値と同じことを確かめる。

```bash
gcloud run services describe agents --region="$REGION" --format="value(status.url)"
```

## 5. web をデプロイする（公開。Direct VPC egress で金庫へ）

```bash
gcloud run deploy web --region="$REGION" --image="${REPO}/app:$(git rev-parse --short HEAD)" --service-account="$WEB_SA" --allow-unauthenticated --min-instances=1 --max-instances=1 --no-cpu-throttling --concurrency=200 --memory=1Gi --cpu=1 --timeout=300 --network=vault-vpc --subnet=run-egress-subnet --vpc-egress=private-ranges-only --set-secrets="SESSION_SIGNING_KEY=session-signing-key:latest" --set-env-vars="VAULT_TEE=true,VAULT_BASE_URL=https://10.10.0.10:8443,VAULT_SERVICE_ACCOUNT=${VAULT_SA},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_LOCATION=global,ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false,GITHUB_REPO_URL=<GitHub のリポジトリの URL>"
```

- `--allow-unauthenticated` は公開の意味。最初は付けずにデプロイし、ID トークンつきの `curl` で `/health` を確かめてから公開に切り替えてもよい（`gcloud run services add-iam-policy-binding web --member=allUsers --role=roles/run.invoker`）。
- イメージの CMD（`uvicorn web.app:create_app_from_env --factory --workers 1`）をそのまま使う。
- `GITHUB_REPO_URL` は、画面の「コミットへのリンク」の土台（点 6）。公開リポジトリの URL を入れる。

死活確認。

```bash
curl -s -o /dev/null -w "%{http_code}\n" "${WEB_URL}/health"
```

## 6. Firestore の TTL ポリシー（6 本。作成に数分かかる）

```bash
gcloud firestore fields ttls update ttl_at --collection-group=stages --database='(default)' --enable-ttl --async
```

```bash
gcloud firestore fields ttls update ttl_at --collection-group=llm_call_counters --database='(default)' --enable-ttl --async
```

```bash
gcloud firestore fields ttls update ttl_at --collection-group=rate_limits --database='(default)' --enable-ttl --async
```

```bash
gcloud firestore fields ttls update ttl_at --collection-group=negotiations --database=vault-db --enable-ttl --async
```

```bash
gcloud firestore fields ttls update ttl_at --collection-group=events --database=vault-db --enable-ttl --async
```

```bash
gcloud firestore fields ttls update ttl_at --collection-group=idempotency --database=vault-db --enable-ttl --async
```

状態を見る（`ACTIVE` になるまで数分）。

```bash
gcloud firestore fields ttls list --database='(default)' --format="table(name,ttlConfig.state)"; gcloud firestore fields ttls list --database=vault-db --format="table(name,ttlConfig.state)"
```

## 7. Cloud Run のリクエストログを残さない（I-9）

`_Default` シンクに除外を足す（URL に依頼者 ID・交渉 ID が入るため）。

```bash
gcloud logging sinks update _Default --add-exclusion="name=run-requests,filter=LOG_ID(\"run.googleapis.com/requests\")"
```

## 8. Vertex AI のキャッシュを無効にする（spec〔29〕・R-9。オーナーで実行）

```bash
curl -s -X PATCH -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" "https://aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/cacheConfig" -d "{\"name\":\"projects/${PROJECT_ID}/cacheConfig\",\"disableCache\":true}"
```

```bash
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" "https://aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/cacheConfig"
```

## 9. デプロイの確認（全項目）

```bash
PROJECT_ID="$PROJECT_ID" REGION="$REGION" ZONE="$ZONE" WEB_URL="$WEB_URL" AGENTS_URL="$AGENTS_URL" VAULT_MODE=tee PROJECT_NUMBER="$PROJECT_NUMBER" EXPECTED_KMS_PRINCIPALS_FILE=tmp/tee_spike/expected-kms-principals.json bash scripts/deploy_check.sh
```

- `vertex-quota`・`submission-checklist` などコンソールで見る項目は SKIP で出る。`tee-i`（拒否ポリシー）は、手順 G が済むまで NG。
- `--reset-vault` は付けない（金庫を停止→開始する。vTPM のロックアウトのカウンタが 1 増える）。

## 10. 画面と attestation（点 6）

```bash
uv run python scripts/verify_attestation.py --web "$WEB_URL"
```

ブラウザで `WEB_URL` を開き、入口・デモ（ケース 1）・攻撃・面談を一通り動かす（`tests/manual/ui_checklist.md`）。TEE の表示（attestation の内容と GitHub のコミットへのリンク）が出ること。
