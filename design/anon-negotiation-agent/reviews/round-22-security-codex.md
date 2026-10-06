> critic: codex/gpt-6.1-sol reasoning effort xhigh（CLI 0.160.0。455 秒。ログは round-22-security-codex.log）— 反証 22 巡目（安全性。v24 の修正の確認。上限の 3 巡目）。新しい指摘の ID は台帳で X-91・X-92 を振る

欠陥は **2件、いずれも medium** です。静的確認では **C-72＝X-89・X-88・X-90 は直った**。C-73 の通常の GET は直っていますが、読み取り増幅に次の抜け道が残ります。

1. **GET 専用・存在しない経路への POST が、枠外で Firestore を読む**

   **種別／重大度:** 実装／medium  
   **判定:** 直っていない。405 の対策は GET・POST 以外しか拒否しません。

   **破綻シナリオ:** `/start` で取得した有効なクッキーと `X-Requested-With` を付け、本文なしの `POST /me` または `POST /no/such/route` を繰り返す。読み取り枠は GET だけを数えるため、毎回セッションのロックと `meta.touch()` が走り、Firestore を読んだ後で405／404になる。GET の120回を使い切っていても続けられます。複数クッキーで並行すれば、依頼者ごとの直列化も回数の歯止めになりません。

   **該当箇所:** [limits.py:570](/Users/toshixa/dev/tenshokuagent/src/web/limits.py:570)、[session_middleware.py:67](/Users/toshixa/dev/tenshokuagent/src/web/session_middleware.py:67)・[同:87](/Users/toshixa/dev/tenshokuagent/src/web/session_middleware.py:87)、[principals_meta.py:145](/Users/toshixa/dev/tenshokuagent/src/web/principals_meta.py:145)。

   **直し方の方向:** 経路とメソッドの一致をセッション確認より前に確かめ、404／405を先に返す。セッション確認の読み取り自体にも、メソッドに依存しないメモリの枠を設ける。

2. **除外された `/start`・入口の注記が、セッション確認の読み取りを無制限に残す**

   **種別／重大度:** 設計／medium  
   **判定:** 直っていない。v24どおりの実装ですが、費用抑制の残課題です。

   **破綻シナリオ:** 有効なクッキーで `GET /v1/interview/notice` を繰り返すと、固定文を返す前に毎回 `touch()` が Firestore を読む。`GET /start` もセッション確認が `session_start` の判定より先なので、20回の枠を超えて429になった要求でも読み出しが続きます。「1時間に1回」は書き込みの間引きで、読み取りの間引きではありません。

   **該当箇所:** [design.md:914](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:914)、[app.py:112](/Users/toshixa/dev/tenshokuagent/src/web/app.py:112)、[api.py:328](/Users/toshixa/dev/tenshokuagent/src/web/api.py:328)、[session_middleware.py:87](/Users/toshixa/dev/tenshokuagent/src/web/session_middleware.py:87)。

   **直し方の方向:** 注記はセッション確認を行わず返す。`/start` は必要な確認より前にメモリの枠を掛ける。**上限なく課金対象の読み取りを増やせるため、安全性上は medium の未解決として扱います。** 受容するなら、この残りを明記した人間判断が必要です。

SSE・本人の GET・ページ・未知の path への GET は、セッション確認より前で数えられます。本文は先読み後に渡され、10秒の期限は本文全体に掛かります。64 KB の保持について、追加のメモリ DoS 欠陥は今回の静的確認では確定していません。

ネットワーク接続、試験実行、指定された秘密の場所の読み取りは行っていません。解決済み索引の論点は再提起していません。

