# TEE スパイクの取り決め（契約）v1

- 日付: 2026-10-03。対象: 10/3〜4 のスパイク（design.md §9、研究報告 `research/tee-spike.md`）。
- 役割: 3 つの作業パッケージ（A 金庫、B web と共有の検証部品、C イメージと手順）が、同時に別の worktree で作る。**ここに書いた名前・形・振る舞いを、3 つとも同じにする**。契約を変える必要が出たら、変えずに報告する（呼び出し側が決める）。
- 読み手: 実装者（サブエージェント）と、あとで設計書 §9・§3.3・§10 に写す呼び出し側。

## 0. 守ること（全パッケージ共通）

- 書き換えてよいのは、自分のパッケージの表にあるファイルだけ。`design/`・`ledger.md`・`state.json`・`pyproject.toml`・`uv.lock`・`config/params.toml` は書き換えない（読んでよい）。
- 新しい依存を足さない。直接 import してよい追加のライブラリは `cryptography`（AES-GCM・X.509）と `google-auth`（`google.auth.jwt`・`google.auth.crypt`）だけ（どちらも uv.lock にある。pyproject への明示は呼び出し側が、ユーザーの承認の後に行う）。`requests`・`aiohttp`・`google-cloud-kms`・`pyOpenSSL` は使わない。HTTP は `httpx`。
- GCP には接続しない。テストは、偽の launcher（`httpx.MockTransport`）、手元で作った RSA 鍵で署名した偽のトークン、Firestore エミュレータ（conftest が起動する）だけで動かす。`gcloud` も叩かない。
- 秘密を触らない: `~/.ssh`・`~/.aws`・`~/.config/gcloud`・`.env`・`*credentials*.json` などは `ls` もしない。
- ログ・例外の文・テストの出力に、トークンの値・DEK・鍵・依頼者 ID・交渉 ID を書かない（design.md §3.8、台帳 X-40）。書いてよいのは claim の値（`image_digest`・`hwmodel` など）、ステータス、例外の型名。
- コミットしない。一時ファイルは scratchpad に、自分の接頭辞（`tee-a-`・`tee-b-`・`tee-c-`）を付けて置く。
- 完了の報告は「主張」として扱われる。実行したコマンドと、その出力の要点（合格数）を報告に書く。

## 1. 共有の設定 `[vault.tee]`（`config/params.toml`。呼び出し側が置いた。読むだけ）

| キー | 値 | 使う側 |
|---|---|---|
| `port` | 8443 | A（受信）、C（`EXPOSE`） |
| `attestation_audience` | `https://vault.anon-nego.internal/attestation` | A（launcher に求める aud）、B（検証する aud） |
| `caller_audience` | `https://vault.anon-nego.internal` | A（呼び出し元の ID トークンの aud）、B（web が ID トークンに付ける aud。手順の `VAULT_AUDIENCE` と同じ） |
| `caller_service_account` | `web-run` | A（許す呼び出し元。メールは `<名前>@<プロジェクト ID>.iam.gserviceaccount.com`） |
| `workload_identity_pool` / `workload_identity_provider` | `vault-tee-pool` / `attestation-verifier` | A（STS の audience） |
| `kms_key_ring` / `kms_key` | `vault-tee` / `vault-kek` | A（鍵の名前） |
| `launcher_socket` | `/run/container_launcher/teeserver.sock` | A |
| `claims_token_file` | `/run/container_launcher/attestation_verifier_claims_token` | A |
| `min_attestation_interval_seconds` | 1.0 | A（`/v1/attestation` が launcher を呼ぶ最短の間隔） |
| `tls_certificate_days` | 90 | A |
| `tls_dir` | `/dev/shm/vault-tls` | A |
| `allowed_hwmodels` | `["GCP_AMD_SEV", "GCP_INTEL_TDX"]` | B（検証ポリシーの既定） |
| `attestation_issuer` | `https://confidentialcomputing.googleapis.com` | B |
| `attestation_signer_certs_url` | `https://www.googleapis.com/service_accounts/v1/metadata/x509/signer@confidentialspace-sign.iam.gserviceaccount.com` | B（`kid` → PEM） |
| `caller_certs_url` | `https://www.googleapis.com/oauth2/v1/certs` | A（`kid` → PEM） |

- プロジェクト ID・プロジェクト番号・ゾーン・インスタンス名は、設定にもコードにも書かない。金庫は Compute Engine のメタデータサーバから実行時に取る（§4）。web は環境変数で受ける（§7）。
- A は `src/vault/config.py` に `VaultTeeConfig`（この節のキー）を足して読む。B は `src/negotiation_core/tee_settings.py` に `load_tee_settings()` を作って同じ節を読む（web とスクリプトが使う）。二重に読むのは承知の上（スパイク）。

## 2. 用語と形

- **nonce**: 検証する側（web・スクリプト）が毎回作る乱数。32 バイトを base64url（詰め物なし）にした 43 文字。受け付ける形は正規表現 `^[A-Za-z0-9_-]{16,74}$`（launcher の制限は 1 個 10〜74 バイト、最大 6 個）。
- **certificate_sha256**: 金庫が TLS で出す葉の証明書の **DER** の SHA-256。小文字の 16 進 64 文字。金庫は `cert.public_bytes(Encoding.DER)` から、web は `ssl.get_server_certificate` → `ssl.PEM_cert_to_DER_cert` から計算する。
- **attestation トークン**: launcher が返す JWT（`token_type: "OIDC"`）。`aud` は `attestation_audience`、`eat_nonce` は要求した nonce の並び（1 個なら文字列、複数なら文字列の配列。**どちらの形も受け付ける**）。
- **呼び出し元の ID トークン**: web のサービスアカウントの Google ID トークン。`aud` は `caller_audience`。

## 3. 金庫の attestation の口（A が作り、B が呼ぶ）

`GET /v1/attestation?nonce=<nonce>`

- 認証なし（web は、この口の応答を確かめるまで金庫を信用しないので、ID トークンをまだ送らない。トークンを未検証の相手に渡さないため）。VPC のファイアウォールと IAP の範囲だけが届く前提。
- 金庫は launcher に `POST http://localhost/v1/token`（Unix ソケット `launcher_socket`。`httpx.Client(transport=httpx.HTTPTransport(uds=...))`）、本文 `{"audience": <attestation_audience>, "token_type": "OIDC", "nonces": [<nonce>, <certificate_sha256>]}`。応答の本文（文字列）がトークン。
- 応答 200: `{"token": "<jwt>", "certificate_sha256": "<hex64>"}`（`application/json`）。
- 400 `{"detail": "invalid nonce"}`（形が違う）。429 `{"detail": "attestation rate limited"}`（前回の launcher 呼び出しから `min_attestation_interval_seconds` 未満）。503 `{"detail": "attestation unavailable"}`（launcher に届かない・2xx 以外・空）。detail は固定文。
- Cloud Run 版（attestation の部品を渡さない `create_app`）には、この口を付けない（404 のまま）。

## 4. 金庫の TEE 版の起動口（A）

`python -m vault.tee.main`。環境変数は使わない（launch policy で `tee-env-*` を許さない）。順序:

1. `mask_ids_in_logs()`。ログは INFO、標準出力。
2. メタデータサーバ（`http://metadata.google.internal/computeMetadata/v1/`、ヘッダ `Metadata-Flavor: Google`）から `project/project-id`・`project/numeric-project-id`・`instance/zone`（`projects/<番号>/zones/<ゾーン>` の最後の要素）・`instance/name` を取る。リージョンはゾーンの末尾 `-x` を除いたもの。
3. 鍵の解放（§5）。失敗したら非 0 で終了する（`tee-restart-policy=OnFailure` が再起動する）。
4. Firestore `vault-db` の `VaultStore`（既存の `vault.firestore_client.create_client()`。SA の既定の認証）。
5. 封印の自己試験（§6）: 文書 `_tee/selftest` の項目 `probe` に封印した乱数を書き、読み戻して開封し、一致を確かめる。ログ `sealing self-test ok`。
6. TLS（§8）: 鍵と自己署名の証明書を作り、`tls_dir`（0700）に `key.pem`・`cert.pem`（0600）で書く。
7. `create_app(store, caller_verifier=<§9>, attestation=<§3 の部品>)`。
8. `uvicorn.run(app, host="0.0.0.0", port=<port>, ssl_keyfile=..., ssl_certfile=...)`。SIGTERM は uvicorn に任せる。

`create_app(store, *, caller_verifier=None, attestation=None)` の既定（両方 None）は、Cloud Run 版そのまま（既存のテストが変わらない）。

## 5. 鍵の解放（A。`google-auth` の `identity_pool` も `requests` も使わず、httpx で 2 つの REST を直接呼ぶ）

1. `claims_token_file` を読む（launcher が約 1 時間ごとに書き直す既定のトークン。中身はログに出さない）。
2. STS で交換: `POST https://sts.googleapis.com/v1/token`、JSON
   `{"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "audience": "//iam.googleapis.com/projects/<番号>/locations/global/workloadIdentityPools/<pool>/providers/<provider>", "scope": "https://www.googleapis.com/auth/cloud-platform", "requested_token_type": "urn:ietf:params:oauth:token-type:access_token", "subject_token_type": "urn:ietf:params:oauth:token-type:jwt", "subject_token": "<1 の中身>"}` → `access_token`。
3. KMS: 鍵の名前 `projects/<プロジェクト ID>/locations/<リージョン>/keyRings/<kms_key_ring>/cryptoKeys/<kms_key>`。`POST https://cloudkms.googleapis.com/v1/<鍵の名前>:encrypt` `{"plaintext": <base64>}` → `ciphertext`、`:decrypt` `{"ciphertext": <base64>}` → `plaintext`。`Authorization: Bearer <access_token>`。
4. DEK の保管: `vault-db` の文書 `_tee/dek`。無ければ 32 バイトの乱数を作って encrypt し、`{"wrapped_dek": <bytes>, "kek": <鍵の名前>, "created_at": <サーバ時刻>, "image_digest": <既定トークンの submods.container.image_digest を未検証で読んだ値。読めなければ None>}` を `create()` で書く（すでにあれば読み直す）。あれば `wrapped_dek` を decrypt する。DEK はメモリにだけ置く。
5. 失敗の扱い: STS・KMS の 4xx は「条件に合わない」としてステータスだけをログに書き、終了。5xx・通信エラーは 3 回まで（2 秒・4 秒・8 秒）やり直してから終了。

## 6. 封印（A。`src/vault/tee/sealing.py`）

- `class Sealer(dek: bytes)`（32 バイト以外は ValueError）。`seal(path: str, field: str, plaintext: bytes) -> bytes`、`open(path: str, field: str, sealed: bytes) -> bytes`。
- AES-256-GCM（`cryptography.hazmat.primitives.ciphers.aead.AESGCM`）。nonce は 12 バイトの乱数で、出力は `nonce || 暗号文(タグ込み)`。AAD は `f"{path}#{field}".encode()`（`path` は Firestore の文書パス。例 `principals/ab12...`）。
- 開封の失敗（タグ不一致・短すぎる・AAD 違い）は `SealError`（ValueError の一種。平文も暗号文も文に入れない）。
- `class NoopSealer`: 同じ API で、そのまま返す（Cloud Run 版・テスト用）。
- `store.py` への組み込みは範囲外（10/5 以降）。

## 7. web の TEE モード（B）

環境変数:

| 変数 | 意味 |
|---|---|
| `VAULT_TEE` | `true` で TEE モード（既定 `false`。`true`・`false` 以外は起動を拒否） |
| `VAULT_BASE_URL` | 既存。TEE では `https://10.10.0.10:8443` |
| `VAULT_SERVICE_ACCOUNT` | 金庫の SA のメール（トークンの `google_service_accounts` と照合。TEE モードでは必須） |
| `GOOGLE_CLOUD_PROJECT` | 既存。トークンの `submods.gce.project_id` と照合（TEE モードでは必須） |
| `VAULT_RELEASES_FILE` | digest の許可リスト（既定 `deploy/vault-releases.json`。無ければ起動を拒否） |
| `VAULT_EXPECTED_ZONE` / `VAULT_EXPECTED_INSTANCE` | 任意。あれば照合 |
| `GITHUB_REPO_URL` | 任意。コミットのリンクの土台（例 `https://github.com/<owner>/<repo>`） |

TEE モードでは: (1) 金庫への ID トークンの audience を `caller_audience` に固定する（`IdTokenAuth(provider, service_url, audience=...)` を足す。既定は従来どおり URL から）。(2) 金庫の `httpx.AsyncClient` に、§8 の「検証してからピン留めする」transport を付ける。`VaultClient` は変えない。

## 8. 検証してからピン留め（B。`src/web/attested_transport.py`）

`class AttestedVaultTransport(httpx.AsyncBaseTransport)`:

1. 最初の要求のとき（と、付け替えのとき）: `ssl.get_server_certificate((host, port))` を `asyncio.to_thread` で呼び、PEM → DER → `certificate_sha256`。
2. その 1 枚だけを信用する `SSLContext`（`ssl.create_default_context(cadata=pem)`、`check_hostname=False`、`verify_mode=CERT_REQUIRED`）で、`GET /v1/attestation?nonce=<新しい nonce>` を呼ぶ（認証ヘッダなし）。
3. 返った `token` を `negotiation_core.attestation.verify_attestation_token(..., nonce=nonce, certificate_sha256=<1 で計算した値>)` で確かめる（応答の `certificate_sha256` は参考値。**自分で計算した値**と照合する）。
4. 通ったら、その `SSLContext` を持つ `httpx.AsyncHTTPTransport(verify=ctx)` に以後の要求を流す。通らなければ `AttestationError` を `httpx.ConnectError` に写して投げる（`VaultClient` が `VaultUnavailableError` にし、レフェリーが待つ）。
5. `httpx.ConnectError`・`ssl.SSLError`・`httpx.RemoteProtocolError` が出たら（金庫の再起動で証明書が変わる）、前回の検証から `min_reverify_interval_seconds`（既定 2.0）以上たっていれば 1〜3 をやり直して、同じ要求を 1 回だけ送り直す。
6. 公開する属性: `last_verification: VerifiedAttestation | None`、`last_verified_at: float | None`、`async def attest(nonce: str) -> tuple[str, VerifiedAttestation]`（web の API が使う。ピン留め済みの接続で §3 を呼び、検証して返す）。

`GET /api/tee/attestation?nonce=`（`src/web/api.py`）:

- TEE モードでなければ 404。
- `nonce` あり（§2 の形。違えば 400）: 金庫へ転送して検証する。前回の転送から 2 秒未満なら 429。
- `nonce` なし: 直近 5 分以内の結果があればそれを返す。無ければ新しい nonce で 1 回だけ行う。
- 応答: `{"verified": bool, "reason": str | null, "checked_at": <ISO 8601>, "nonce": str, "certificate_sha256": str, "claims": {"image_digest", "hwmodel", "swname", "swversion", "dbgstat", "support_attributes", "project_id", "zone", "instance_name"}, "release": {"commit", "url", "built_at"} | null, "token": "<jwt>"}`。失敗のときも、取れた範囲で claims を返す（`verified=false`、`reason` に §10 の理由）。
- 画面（HTML）は範囲外。JSON まで。

## 9. 呼び出し元の検証（A。`src/vault/tee/caller_auth.py`）

- `/v1/attestation` 以外のすべての経路に、FastAPI の依存として掛ける。
- `Authorization: Bearer <token>` を取り、`google.auth.jwt.decode(token, certs=<kid→PEM>, audience=<caller_audience>)`（署名・`exp`・`iat`・`aud`）。さらに `iss` が `accounts.google.com` か `https://accounts.google.com`、`email` が `<caller_service_account>@<プロジェクト ID>.iam.gserviceaccount.com` と一致、`email_verified` が真。
- 401 `{"detail": "unauthenticated"}`: ヘッダなし・Bearer でない・形式不正・署名不正・期限切れ・aud 違い・iss 違い。403 `{"detail": "forbidden"}`: 署名は正しいが `email` が許可された SA でない（`email_verified` が偽も 403）。
- 証明書（`caller_certs_url`）は 1 時間キャッシュ。未知の `kid` のときだけ取り直す（1 分に 1 回まで）。取得に失敗しても、キャッシュが有効なうちは動く。取得は httpx（同期でよい。起動時に 1 回取り、以後はバックグラウンドでなく要求の中で取り直す）。
- テストは、手元で作った RSA 鍵で署名したトークンと、その公開鍵（自己署名の証明書の PEM）を `certs` に渡して行う。

## 10. attestation トークンの検証（B。`src/negotiation_core/attestation.py`。web とスクリプトが共有）

```python
@dataclass(frozen=True)
class AttestationPolicy:
    audience: str                       # attestation_audience
    issuer: str                         # attestation_issuer
    allowed_hwmodels: frozenset[str]
    allowed_digests: frozenset[str]     # deploy/vault-releases.json の digest
    project_id: str | None              # None なら照合しない
    service_account: str | None         # 金庫の SA のメール。None なら照合しない
    zone: str | None = None
    instance_name: str | None = None
    require_production: bool = True     # dbgstat == disabled-since-boot と STABLE を要求する（--allow-debug で False）

class AttestationError(ValueError):   # 属性 reason: 下の理由のどれか
    ...

def verify_attestation_token(token: str, *, certs: Mapping[str, str], policy: AttestationPolicy,
                             nonce: str, certificate_sha256: str | None, now: float | None = None) -> VerifiedAttestation
def decode_claims_unverified(token: str) -> dict   # 表示用。検証には使わない
def load_releases(path) -> list[dict]              # {"releases": [{"digest", "commit", "built_at"}]}
def release_for_digest(releases: list[dict], digest: str) -> dict | None
```

確かめる順と `reason`:

| 順 | 確かめること | reason |
|---|---|---|
| 1 | JWT の形（3 区切り・base64url・JSON） | `malformed` |
| 2 | 署名（`google.auth.jwt.decode(certs=...)`。RS256。`kid` が certs に無ければ取り直して 1 回だけやり直す） | `signature` |
| 3 | `exp`・`iat`（`now` を注入できる。許容のずれ 60 秒） | `expired` |
| 4 | `aud` == policy.audience | `audience` |
| 5 | `iss` == policy.issuer | `issuer` |
| 6 | `eat_nonce`（文字列か配列）に nonce がある | `nonce` |
| 7 | `eat_nonce` に certificate_sha256 がある（None のときは飛ばす。スクリプトの `--web` 経由） | `certificate` |
| 8 | `swname == "CONFIDENTIAL_SPACE"` | `swname` |
| 9 | `require_production` のとき `dbgstat == "disabled-since-boot"` | `debug` |
| 10 | `require_production` のとき `"STABLE" in submods.confidential_space.support_attributes` | `support_attributes` |
| 11 | `hwmodel in allowed_hwmodels` | `hwmodel` |
| 12 | `submods.container.image_digest in allowed_digests` | `image_digest` |
| 13 | `submods.container.cmd_override`・`env_override` が無いか空 | `override` |
| 14 | `submods.gce.project_id`（policy に値があれば） | `project` |
| 15 | `google_service_accounts` が `{policy.service_account}` と一致（policy に値があれば） | `service_account` |
| 16 | `submods.gce.zone`・`instance_name`（policy に値があれば） | `zone`・`instance` |

`VerifiedAttestation`（frozen dataclass）: `image_digest, hwmodel, swname, swversion, dbgstat, support_attributes (tuple[str, ...]), project_id, zone, instance_name, service_accounts (tuple[str, ...]), issued_at, expires_at, nonces (tuple[str, ...]), claims (dict。全文)`。

署名鍵の取得: `class SignerCerts(url, *, ttl_seconds=3600, min_refetch_interval_seconds=60)`。`get() -> dict[str, str]`、`refresh()`。取得は httpx（同期）。テストは `fetch` を差し替える。

## 11. TLS の自己署名の証明書（A。`src/vault/tee/tls.py`）

- P-256（ECDSA）、SHA-256、有効期間 `tls_certificate_days`、subject/issuer `CN=vault`、SAN に DNS `vault`、`BasicConstraints(ca=False)`、`ExtendedKeyUsage([SERVER_AUTH])`。
- `generate(days: int) -> TlsMaterial(key_pem: bytes, cert_pem: bytes, certificate_sha256: str)`。`write(material, directory: Path) -> tuple[Path, Path]`（ディレクトリ 0700、ファイル 0600。`key.pem`・`cert.pem`）。
- 起動のたびに作り直す（金庫の再起動で証明書が変わるのは設計どおり。web が付け替える）。

## 12. スクリプト（B）

- `scripts/verify_attestation.py`（AC-23）: `--web <web の URL>`（`GET /api/tee/attestation?nonce=` を呼ぶ）か `--direct <金庫の URL>`（IAP トンネルの出口。`GET /v1/attestation?nonce=` を呼び、TLS は検証なしで証明書を控えてハッシュを照合する）。共通: `--releases`（既定 `deploy/vault-releases.json`）、`--project`、`--service-account`、`--hwmodel`（繰り返し可。既定は設定の値）、`--allow-debug`（`require_production=False`。警告を出す）、`--check-commit`（GitHub の API でコミットの存在を確かめる。`GITHUB_REPO_URL` が要る）。全部通れば終了コード 0 と claims の表、1 つでも違えば 1 と `reason`。トークンの値は `--print-token` のときだけ出す。
- `scripts/tee_probe_client.py`（Cloud Run Job。スパイクだけで使う）: 環境変数 `VAULT_BASE_URL`・`VAULT_SERVICE_ACCOUNT`・`GOOGLE_CLOUD_PROJECT`（無ければメタデータサーバの `project/project-id`）・`VAULT_RELEASES_FILE`・`TEE_PROBE_ALLOW_DEBUG`（`true` で `require_production=False`）。順に: (1) メタデータサーバから `caller_audience` の ID トークンを取り、claim の `iss`・`aud`・`azp`・`email`・`email_verified`・`exp` を出力する（トークンの値は出さない）。(2) §8 の手順で金庫を検証し、結果と claims を出力する。(3) ピン留めした接続で `GET /v1/principals/0000000000000000/policy` を Bearer つきで呼び、ステータスを出力する（404 が期待）。(4) 同じ経路を Bearer なしで呼び、ステータスを出力する（401 が期待）。(2) が通り、(3) が 404、(4) が 401 なら終了コード 0、それ以外は 1。

## 13. イメージとビルド（C）

- `Dockerfile.vault`: `FROM python:3.12-slim`（digest 固定は L2 の課題。タグのまま）。`COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/`。`ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_PREFERENCE=only-system UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONPATH=/app/src PYTHONUNBUFFERED=1`。`WORKDIR /app`。`pyproject.toml`・`uv.lock`・`.python-version` を先に `COPY` して `uv sync --frozen --no-dev`。次に `src/vault`・`src/negotiation_core`・`config/params.toml` だけを `COPY`。`EXPOSE 8443/tcp`。`LABEL "tee.launch_policy.log_redirect"="always" "tee.launch_policy.monitoring_memory_allow"="never"`。`ENTRYPOINT ["/opt/venv/bin/python", "-m", "vault.tee.main"]`、`CMD []`。`USER` は書かない（root）。
- `Dockerfile`（web・agents・probe。Cloud Run 用）: 同じ土台で `src/`・`config/`・`scripts/`・`deploy/`・`static/`（あれば）・`fixtures/`（あれば）を `COPY`。`ENV PATH=/opt/venv/bin:$PATH PORT=8080`。`CMD ["uvicorn", "web.app:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]`。
- `cloudbuild.vault.yaml`: substitutions `_IMAGE`・`_COMMIT`。steps: `docker build -f Dockerfile.vault -t ${_IMAGE} --label org.opencontainers.image.revision=${_COMMIT} .` → `docker push ${_IMAGE}`。`images: ["${_IMAGE}"]`。`options: {logging: CLOUD_LOGGING_ONLY}`。
- `.gcloudignore` と `.dockerignore`（同じ中身）: `.git`、`.venv`、`tmp`、`temp`、`design`、`tests`、`__pycache__`、`*.pyc`、`.pytest_cache`、`.env*`、`*credentials*.json`、`*service-account*.json`、`.claude`、`*.md`（ただし `!deploy/**` は残す）。
- `deploy/vault-releases.json`: `{"releases": [{"digest": "sha256:<64 hex>", "commit": "<40 hex>", "built_at": "<ISO 8601>"}]}`（呼び出し側が空の表を置いた）。
- `scripts/tee_record_release.py --digest sha256:... --commit <sha> [--releases deploy/vault-releases.json] [--built-at <ISO>]`: 形を検査して追記する。同じ digest があれば上書きせずに終了コード 0 で知らせる。
- `scripts/tee_reset_dek.py --yes`: 手元の ADC で `vault-db` の `_tee/dek` と `_tee/selftest` を消す（debug イメージの間に作った DEK を、本番イメージで作り直すため。`--yes` が無ければ何もせずに説明だけ出す）。
- `tests/manual/tee-spike.md`: 研究報告の「点ごとの合否の基準」を、ユーザーが手順書どおりに実行して確かめる形（コマンドと、何が出れば合格か。貼ってもらう出力）に写す。
- 自動テスト `tests/test_tee_image_files.py`: `Dockerfile.vault` に `EXPOSE 8443/tcp`・2 つの LABEL・`ENTRYPOINT`・`CMD []` があり `USER` が無いこと、`cloudbuild.vault.yaml` が YAML として読めて `_IMAGE`・`_COMMIT` を使うこと、`.gcloudignore` と `.dockerignore` が同じで `design`・`tests`・`.env` を除くこと、`deploy/vault-releases.json` が形どおりなこと。`tests/test_tee_record_release.py`: 追記・重複・形の違反。

## 14. テストの置き場と実行

| パッケージ | テスト | 実行 |
|---|---|---|
| A | `tests/test_tee_caller_auth.py`・`tests/test_tee_sealing.py`・`tests/test_tee_tls.py`・`tests/test_tee_attestation_api.py`・`tests/test_tee_key_release.py` | `uv run pytest tests/test_tee_*.py tests/test_vault_*.py tests/test_api_smoke.py -q` |
| B | `tests/test_attestation_verification.py`・`tests/test_attested_transport.py`・`tests/test_verify_attestation.py`・`tests/test_web_tee_api.py`・`tests/test_service_auth.py`（既存に足す） | `uv run pytest tests/test_attest*.py tests/test_verify_attestation.py tests/test_web_tee_api.py tests/test_service_auth.py tests/test_web_api.py -q` |
| C | `tests/test_tee_image_files.py`・`tests/test_tee_record_release.py` | `uv run pytest tests/test_tee_image_files.py tests/test_tee_record_release.py -q` |

最後に全体 `uv run pytest -q` が通ること（既存 1,498 件を壊さない）。

## 15. 追記（2026-10-03。批評 16 巡目 X-67 の受理。パッケージ B に伝達済み）

- §8 の検証（初回のピン留めと、接続エラー後の付け替え）は **transport 単位の single-flight** にする。同時に来た要求は同じ検証の結果を待って共有し、それぞれが `/v1/attestation` を呼ばない。検証が失敗したら、待っていた要求にも同じ失敗（`httpx.ConnectError`）を返す。再検証の間隔の下限（2 秒）は single-flight の後に適用する（直前の検証が間隔内なら、待たずにその結果を使う）。
- `GET /api/tee/attestation` は、`nonce` なしなら検証済みの直近の結果（5 分）を返すのが既定で、金庫の発行枠（毎秒 1 回）を使うのは `nonce` ありの転送だけ（2 秒の制限）。
- 理由: 起動時に見回りが複数の交渉を同時に再開すると、single-flight がなければ 1 件以外が金庫の 429 に当たる。

## 16. 追記（2026-10-03。批評 16・17 巡目の受理分。実装者 A・B・C に個別に伝達済み。契約の本文より優先する）

- **(C-56) §5**: `_tee/dek` に `kek_version`（KMS の `:encrypt` の応答の `name`。鍵の版の完全な名前）を足す。起動時に `GET https://cloudkms.googleapis.com/v1/<鍵の名前>` の `primary.name` と完全一致しなければ、復号せずに非 0 で終了する（ログは固定文 `DEK was wrapped by a non-primary key version; rotate the DEK` と 2 つの版の名前）。`kek_version` の無い文書も拒否する。
- **(X-70) 手順の順序**: debug の VM を消す → 鍵の新しい版を primary にする → 古い版を無効化する → `scripts/tee_reset_dek.py --yes` → 本番の VM を作る（初回の起動で、新しい版で DEK を作る）。本番の起動後に版を回さない。live のデータが入った後の版の更新は、動いている金庫が、起動時に開いた DEK を primary で包み直して `_tee/dek` を上書きする（1 時間ごとに primary を確かめる。`kek_version` の前提条件つきの更新）。これはスパイクの範囲外（10/5 以降）。起動時の規則（primary でなければ起動しない）は変えない。
- **(X-70) §4 の 5**: `_tee/selftest` は、無ければ作り（32 バイトの乱数 R を封印した `probe`、R の SHA-256 の `probe_sha256`、`created_at`）、あれば `probe` を開封して SHA-256 が一致することを確かめる。一致しなければ非 0 で終了（固定文 `sealing self-test failed: existing ciphertext does not open`）。再起動をまたいで既存の暗号文が同じ DEK で開くことの確認。
- **(C-57) §10・§13**: `deploy/vault-releases.json` の各要素に `status`（`active`／`revoked`）。`load_releases` は無ければ `active`。`allowed_digests` は `active` だけ。`release_for_digest` は revoked も返す（表示用）。`scripts/tee_record_release.py --revoke <digest>` で `revoked` と `revoked_at`。許可表はイメージに焼くので、失効は表を直して `web` を再デプロイする（新しいリビジョン）。
- **(C-57) §8**: 接続エラーのときだけでなく、`reverify_interval_seconds`（既定 600）ごとにも検証し直す。間隔を過ぎた最初の要求が（single-flight で）再検証し、失敗したらピンを外して、通るまで全要求を `httpx.ConnectError` にする。
- **(X-67) §8**: 検証（初回・付け替え・定期）は transport 単位の single-flight（§15）。
- **(L16-5) §7**: `web` の ID トークンは、メタデータサーバから `format=full` で取る（`identity?audience=...&format=full`。これがないと `email` が入らない）。
- **(L16-6・X-73) §12**: `scripts/verify_attestation.py` は `--project`・`--service-account` を必須にする。`--direct` は、控えた証明書だけを信用する接続で `/v1/attestation` を呼び、自分で計算した証明書ハッシュで `eat_nonce` を照合する。`--web` は金庫の証明書を観測できないので、nonce・署名・claims・`active` なダイジェストまでを確かめる（証明書との結び付きは確かめない。設計書 AC-23 もそう書く）。
- **(X-71) deploy_check（スパイクの後）**: KEK の実効権限は、Cloud Asset の Policy Analyzer（`gcloud asset analyze-iam-policy --full-resource-name=//cloudkms.googleapis.com/<鍵の名前> --permissions=cloudkms.cryptoKeyVersions.useToDecrypt,cloudkms.cryptoKeyVersions.useToEncrypt`）で全階層・custom role 込みで列挙し、`active` なダイジェストの principalSet 以外が 1 件でもあれば失敗にする。WIF プロバイダは attribute mapping と condition を組で完全一致で照合する。
