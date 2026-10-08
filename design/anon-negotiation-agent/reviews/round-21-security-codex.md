> critic: codex/gpt-6.1-sol reasoning effort xhigh（CLI 0.160.0。724 秒。ログは round-21-security-codex.log）— 反証 21 巡目（安全性。v23 の修正の確認）。新しい指摘の ID は台帳で X-88〜X-90 を振る

欠陥は3件、いずれも **medium** です。指定差分を静的に確認しました。ネットワーク接続・試験実行・禁止箇所の読み取りは行っていません。

1. **祖先の取得に失敗すると、不完全な範囲の IAM 解析を合格にする**
   
   **種別／重大度:** 実装／medium  
   **判定:** X-86 は直っていない。継承照合は追加されたが、取得失敗時に抜け道が残る。
   
   **破綻シナリオ:** 組織配下のプロジェクトで `ORG_ID` が未設定、祖先取得が権限不足などで失敗すると、`analysis_scope()` は拒否せず「プロジェクトのみ」を返す。その範囲の結果が `fullyExplored=true` で期待集合と一致すれば、組織・フォルダから継承された想定外の呼び出し主体を見落として `iam-agents` が合格する。結果として、web の制限や LLM 予算を通らない呼び出しを許し得る。
   
   **該当箇所:** [deploy_check.sh:802](/Users/toshixa/dev/tenshokuagent/scripts/deploy_check.sh:802)、[deploy_check.sh:1403](/Users/toshixa/dev/tenshokuagent/scripts/deploy_check.sh:1403)。祖先取得失敗でも合格する挙動を [test_deploy_check.py:1076](/Users/toshixa/dev/tenshokuagent/tests/test_deploy_check.py:1076) が固定している。  
   **直し方の方向:** 祖先取得・解析失敗は NG とし、組織外であることを確認できた場合だけプロジェクト範囲を許す。

2. **匿名 SSE の再接続で、読み取りの回数制限を迂回できる**
   
   **種別／重大度:** 設計／medium  
   **判定:** C-68 は直っていない。通常のデモ GET の枠は直ったが、SSE 経由が残る。
   
   **破綻シナリオ:** `/v1/stream/demo/negotiations/{nid}/activity` は匿名読み取り枠から除外されている。完了済み交渉を `after_seq=0` で開くと、開始時と最初の読み出しで Firestore・金庫を読み、`final_result` を送って直ちに閉じ、同時接続枠を返す。これを順次繰り返せば、同時2本以内でも毎分120回の枠を超えて読み取りを増幅できる。形の正しい存在しない `nid` でも、Firestore の確認後に403となって枠が戻るため、反復できる。
   
   **該当箇所:** [api.py:109](/Users/toshixa/dev/tenshokuagent/src/web/api.py:109)、[ui_api.py:371](/Users/toshixa/dev/tenshokuagent/src/web/ui_api.py:371)、[ui_api.py:244](/Users/toshixa/dev/tenshokuagent/src/web/ui_api.py:244)。  
   **直し方の方向:** SSE の開始・再接続にも、最初の Firestore・金庫読み取りより前で送信元ごとの時間窓の枠を掛ける。同時接続数の上限は併用する。

3. **宣言なしの本文上限が、セッション処理や枠の評価より後になる**
   
   **種別／重大度:** 実装／medium  
   **判定:** X-85 は直っていない。宣言ありの早期拒否と自動 JSON 解析前の容量上限は直ったが、要求された処理順を満たさない。
   
   **破綻シナリオ:** `Content-Length` がないと、外側のミドルウェアは本文を読まず、内側を呼ぶ。有効な匿名セッションクッキー付きの巨大なチャンク本文では、413になる前にセッションのロックと Firestore の `touch` が走る。`begin` の自動解析で拒否されれば入口枠には到達せず、この読み取りを反復できる。手動で本文を読む `salary/answers` では枠が先に評価され、本文を使わない `discard` では上限判定なしに状態変更まで進む。
   
   **該当箇所:** [body_limit.py:65](/Users/toshixa/dev/tenshokuagent/src/web/body_limit.py:65)、[session_middleware.py:80](/Users/toshixa/dev/tenshokuagent/src/web/session_middleware.py:80)、[interview/api.py:138](/Users/toshixa/dev/tenshokuagent/src/web/interview/api.py:138)。  
   **直し方の方向:** 外側で本文を上限内まで読み終えて検証し、その後にセッション・枠・ルートへ渡す。上限内の本文を再送する方式とし、遅い送信による占有を防ぐ読み取り期限も設ける。

C-69／X-87、C-70、C-71、I-39 は静的確認では直った。C-67 の変更と、組織外を確認した場合の拒否ポリシーの SKIP には、安全性の反論はありません。I-40 の例外を同一プロジェクトの2主体に限定する実装は直った一方、「利用者はなりすませない」というクラウド側の前提は、今回のローカル資料だけでは確認できません。