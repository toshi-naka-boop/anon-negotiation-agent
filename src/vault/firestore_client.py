"""vault-db への Firestore クライアントの作成(design.md §1.1)。

`vault` 専用のデータベース `vault-db` を使う((default) は web 用)。接続先(本物の GCP か
エミュレータか)は呼び出し側の環境(`FIRESTORE_EMULATOR_HOST` の有無)で決まる。テストでは
tests/conftest.py が、クライアントを作る前に必ずこの環境変数をエミュレータへ向ける。
"""

from google.cloud import firestore

VAULT_DATABASE = "vault-db"


def create_client(project: str | None = None, database: str = VAULT_DATABASE) -> firestore.Client:
    """project・database を指定して Firestore クライアントを作る。

    project を省くと、環境から決まる(Cloud Run では、サービスの属するプロジェクト。エミュレータ
    (FIRESTORE_EMULATOR_HOST)では、環境変数 GOOGLE_CLOUD_PROJECT か、ライブラリの既定の名前)。
    """
    return firestore.Client(project=project, database=database)
