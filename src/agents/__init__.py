"""agents: 交渉エージェントの A2A 受信口と、レフェリー(web)から呼ぶクライアント(design.md §4.2・§4.3)。

- 受信口(サーバ): `agents.app.create_app()`。候補者側・求人側・攻撃モードの求人側の 3 つ。
- クライアント: `agents.client.send_turn()`。web(レフェリー)が 1 手ごとに呼ぶ。

このパッケージの import では、サーバ(a2a-sdk・ADK)を読み込まない。web はクライアントだけを
import すればよい。
"""
