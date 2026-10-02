"""agents: 交渉エージェントの A2A 受信口と、レフェリー(web)から呼ぶクライアント(design.md §4.2・§4.3)。

- 受信口(サーバ): `agents.app.create_app()`。候補者側・求人側・攻撃モードの求人側の 3 つ。それぞれ、呼び出しの種類
  (計画 plan・決定 decide)ごとの LlmAgent を持つ(§4.2)。
- クライアント: `agents.client.send_turn()`。web(レフェリー)が 1 手番に最大 2 回(計画・決定)呼ぶ。返り値は
  `(payload, usage)`(§4.3・台帳 X-58)。

このパッケージの import では、サーバ(a2a-sdk・ADK)を読み込まない。web はクライアントだけを
import すればよい。
"""
