"""クライアント IP の取り方(design.md §8.2「クライアントの見分け方」・台帳 C-3・C-71・調査事項 R-7。契約 research/tee-spike-contract.md §19)。

web は Cloud Run のフロントエンドの後ろで動くので、接続元のアドレスはフロントエンドのものになり、クライアントの IP にならない。
フロントエンドは X-Forwarded-For の末尾に、接続してきたクライアントの IP を追記する。先頭側の値は、利用者が自由に書けるので使わない。
そのため、IP は X-Forwarded-For のカンマ区切りの最後の要素にする。ヘッダがないとき(ローカル・テスト)は、接続元のアドレス。

クライアントごとのレート制限・同時数の枠のキーは、client_key(IPv6 は /64 の接頭辞にまとめる。台帳 C-71)で作る: GET /api/tee/attestation の nonce つきの
転送・Firestore の時間窓カウンタ・SSE の同時本数・面談の同時数・ログインなしの読み取りの枠、のすべて。
"""

import ipaddress

from fastapi import Request

# クライアントの IP が分からないとき(ヘッダも接続元もない)の値。分からない要求は、1 つのキーを共有する(制限の側に倒す)。
UNKNOWN_CLIENT = "unknown"

# IPv6 を「1 つの送信元」として数える接頭辞の長さ(台帳 C-71)。公開 URL は AAAA を返し、回線の利用者には /64 以上が割り当てられ、
# /64 の中のアドレスは利用者が自由に選べる。アドレス単位で数えると、/64 の中で変えるだけで送信元ごとの枠が外れる。
IPV6_KEY_PREFIX_BITS = 64


def client_ip(request: Request) -> str:
    """request を送ってきたクライアントの IP(レート制限のキーの元。キーそのものは client_key)。

    X-Forwarded-For があれば、カンマ区切りの最後の要素(前後の空白は除く)。ヘッダが複数行あるときは、つなげた全体の最後の要素。
    最後の要素が空のとき(ヘッダが空・空白だけ・末尾がカンマなど)は、ヘッダがないものとして扱う: 先頭側の、利用者が書ける値には頼らない。
    ヘッダがなければ、接続元のアドレス。それも分からなければ UNKNOWN_CLIENT。
    """
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    last = forwarded.rsplit(",", 1)[-1].strip()
    if last:
        return last
    return request.client.host if request.client is not None else UNKNOWN_CLIENT


def normalize_client(address: str) -> str:
    """クライアントのアドレスを、枠のキーにする(台帳 C-71)。

    - IPv4 は、アドレス単位(正規の書き方にそろえる)。
    - IPv6 は、/64 の接頭辞(例: 2001:db8:1:2::/64)。同じ /64 の中のアドレスは、書き方(省略・大文字小文字)が違っても同じキーになる。
    - IPv4 射影アドレス(::ffff:203.0.113.5)は、IPv4 に直す。そのまま /64 にすると、IPv4 の利用者がすべて 1 つのキー(::/64)に入ってしまう。
    - IP として読めない値(UNKNOWN_CLIENT・テストの任意の文字列)は、そのまま(unknown は、1 つの共有キーのまま)。
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        host_bits = parsed.max_prefixlen - IPV6_KEY_PREFIX_BITS
        prefix = ipaddress.IPv6Address((int(parsed) >> host_bits) << host_bits)  # 整数で丸める(ゾーン ID の付いたアドレスでも壊れない)
        return f"{prefix.compressed}/{IPV6_KEY_PREFIX_BITS}"
    return str(parsed)


def client_key(request: Request) -> str:
    """request を送ってきたクライアントの、枠のキー(client_ip を normalize_client にかけたもの。台帳 C-71)。"""
    return normalize_client(client_ip(request))
