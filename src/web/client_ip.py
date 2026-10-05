"""クライアント IP の取り方(design.md §8.2「クライアントの見分け方」・台帳 C-3・調査事項 R-7。契約 research/tee-spike-contract.md §19)。

web は Cloud Run のフロントエンドの後ろで動くので、接続元のアドレスはフロントエンドのものになり、クライアントの IP にならない。
フロントエンドは X-Forwarded-For の末尾に、接続してきたクライアントの IP を追記する。先頭側の値は、利用者が自由に書けるので使わない。
そのため、IP は X-Forwarded-For のカンマ区切りの最後の要素にする。ヘッダがないとき(ローカル・テスト)は、接続元のアドレス。

クライアントごとのレート制限のキーに使う(いまは GET /api/tee/attestation の nonce つきの転送。攻撃モードのレート制限も、この関数を使う)。
"""

from fastapi import Request

# クライアントの IP が分からないとき(ヘッダも接続元もない)の値。分からない要求は、1 つのキーを共有する(制限の側に倒す)。
UNKNOWN_CLIENT = "unknown"


def client_ip(request: Request) -> str:
    """request を送ってきたクライアントの IP(レート制限のキー)。

    X-Forwarded-For があれば、カンマ区切りの最後の要素(前後の空白は除く)。ヘッダが複数行あるときは、つなげた全体の最後の要素。
    最後の要素が空のとき(ヘッダが空・空白だけ・末尾がカンマなど)は、ヘッダがないものとして扱う: 先頭側の、利用者が書ける値には頼らない。
    ヘッダがなければ、接続元のアドレス。それも分からなければ UNKNOWN_CLIENT。
    """
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    last = forwarded.rsplit(",", 1)[-1].strip()
    if last:
        return last
    return request.client.host if request.client is not None else UNKNOWN_CLIENT
