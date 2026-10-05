"""vault.tee: 金庫の TEE(Confidential Space)版だけが使う部品(design.md §9。research/tee-spike-contract.md)。

Cloud Run 版の金庫(vault.app.create_app_from_env)は、このパッケージを使わない。TEE 版の起動口は `python -m vault.tee.main`。

- metadata: Compute Engine のメタデータサーバの読み取り(プロジェクト ID・番号・ゾーン・インスタンス名)。
- launcher: launcher の teeserver(Unix ソケット)から、独自の audience・nonce の attestation トークンを取る。
- key_release: 既定の attestation トークンを STS で交換し、Cloud KMS で DEK を包む・解く(鍵の解放)。
- sealing: AES-256-GCM で項目を封印する(Sealer)。
- tls: 起動のたびに作る自己署名の TLS 証明書。
- caller_auth: 呼び出し元(web)の Google ID トークンの検証(FastAPI の依存)。
- attestation_api: `GET /v1/attestation?nonce=`。
- main: 起動の順序。

トークン・DEK・鍵の値は、ログにも例外の文にも書かない(design.md §3.8、台帳 X-40)。書いてよいのは、claim の値・ステータス・例外の型名だけ。
"""
