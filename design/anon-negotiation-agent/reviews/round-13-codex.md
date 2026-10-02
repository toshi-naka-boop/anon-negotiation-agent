結論: 7手番・正常28呼び出しの導出と、既知評価の単調性は閉じています。一方、実装前に直すべき欠陥が5件あります。

1. **X-50 — 種別: 設計 / 重大度: high / 一行タイトル: 数えている単位が、Vertex の物理呼び出しと暦日ごとの支払額に一致しない**

   - **破綻シナリオ:** カウンタは A2A 送信前に増えますが、ADK／Google クライアント内部の再試行があれば、1回の計上で複数回 Vertex を呼べます。また23:59に計上後、停止・輻輳で実送信が0時を越えると、その呼び出しは前日枠のまま、当日枠をさらに1,500回使えます。さらに1呼び出し当たりの入力・思考・出力トークンに設計上の硬い上限がないため、1,500回だけでは金額上限になりません。[design.md §4.1](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:517) はA2Aを数え、[§8.2](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:792) はこれを「支払いの上限」としています。
   - **直し方の方向:** 「1計上＝Vertexの1要求」を保証するため内部再試行を無効化するか、実際のモデル送信点で計上する。日付を越えた計上権は無効にして当日枠を取り直す。入力トークン上限と最大出力・思考量を固定し、価格版を含む最悪金額を呼び出し枠から導く。DV-18には内部再試行、計上後の0時越え、カウンタ障害時のfail-closedを入れる。

2. **X-51 — 種別: 実装 / 重大度: high / 一行タイトル: レフェリーの有効な `check` が、直前の無効手の修正情報だけを消す**

   - **破綻シナリオ:** 決定が無効になり、次の計画は `last_error` を見て確かめを出します。その `check` は連続無効手をリセットしませんが、決定前に読み直したイベント列では最新の手が有効な `check` になるため、`last_error`・`last_invalid` がnullになります。決定LLMは別セッションなので同じ無効手を繰り返し、3回で停止します。既存の組立コードは実際に最新の手だけを見ており、既存テストも「無効手→有効checkならnull」を要求しています。[turn_input.py](/Users/toshixa/dev/tenshokuagent/src/web/turn_input.py:93) [test_turn_input.py](/Users/toshixa/dev/tenshokuagent/tests/test_turn_input.py:130)
   - **直し方の方向:** `last_error` を「最後の有効なエージェント決定以後の無効決定」と定義し、レフェリー内部の `check` では消さない。または計画入力にあったエラーを同じ手番の決定入力へ明示的に引き継ぐ。DV-03・DV-17で、無効決定→有効check→決定の入力にもエラーが残り、修正手が成功することまで検査する。

3. **X-52 — 種別: 設計 / 重大度: medium / 一行タイトル: 費用上限の停止にエージェント手の `end` を流用すると、paused・途中確認・409で終了できない**

   - **破綻シナリオ:** 上限直前の計画後に一時停止が割り込むと、費用ガードが登録する `end` は、`status=active`・`paused=false`・`expected_version`一致という手の前提に阻まれます。[手の前提](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:411) 上限到達後に途中確認へ入った場合も、回答まで「なし」になりません。さらにシステム都合の停止が内部では `ended_by_agent` となり、FR-12の停止判定と運用上の中断の境界が曖昧です。[spec FR-12](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/spec.md:74)
   - **直し方の方向:** `control`／`expire`と同系統の冪等な内部操作 `abort_cost_limit` を設け、`judged` 以外のactive・paused・awaiting_principalすべてから、`expected_version`なしで原子的に「なし」へ終端させる。これは秘密値に依存する停止判定ではなく、取消・期限切れと同じ運用中断だと明記する。DV-18に一時停止、途中確認中、409競合、最終記録が一度だけというケースを追加する。36回目を許すのか、36到達時点で止めるのかも明文化する。

4. **X-53 — 種別: 設計 / 重大度: medium / 一行タイトル: 読むだけの入場制限は `request_id` の冪等性と再起動直後の一覧を扱えない**

   - **破綻シナリオ:** 41件が進行中の状態で、作成応答を失った利用者が同じ `request_id` を再送すると、入場式は既存交渉を返す再送にも新規分の「＋36」を足して拒否できます。これは「同じrequest_idなら既存交渉を返す」と衝突します。[作成の冪等性](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:400) また再起動後、見回りが全ページを読み終える前や、新規作成が一覧へ反映される前は未消化分を過少計上し、「通常は途中停止を起こさない」という入場制限の目的を満たしません。[入場規則](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:794)
   - **直し方の方向:** 最初に金庫の冪等キーを読み、既存なら入場判定を迂回して同じ交渉を返す。新規作成では、起動時の全ページ走査が完了するまで受付を開始しないか、入場時に金庫のopen一覧を完全ページングして保守的に数える。DV-18へ「満杯時の同一request_id再送」「起動直後で見回り未完」「作成直後で次の見回り前」を加える。

5. **X-54 — 種別: 設計 / 重大度: medium / 一行タイトル: DV-11・15・17・18の判定基準に、矛盾と未固定のoracleが残る**

   - **破綻シナリオ:** DV-17は「本人確認が必要は再確認する」と「再起動後、済んだ確認はすべて履歴から埋めて再消費しない」を同時に要求しています。[§4.1](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:508) [DV-17](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:1008) 前者どおりなら、未知評価の後の409では再消費が必要です。DV-11も対象が再評価済みの`last_check`なら履歴キャッシュの欠陥を通らず合格できます。DV-15は冒頭で0.12ドル、本文と合格条件で0.15ドルとなり、思考の基準も「約820」で再現可能な証拠がありません。[冒頭](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:7) [DV-15](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:1006) DV-18も終了時の値だけを見るテストなら、「送信後に計上する」欠陥実装が通ります。
   - **直し方の方向:** 未知評価は「その評価以後にprincipal-answerがなければ再利用可能」と世代番号で判定するか、DV-17の非再消費を安定した2値だけに限定する。DV-11は対象を`last_check`ではない古い履歴にし、金庫呼び出し回数と評価残数の減少を検査する。DV-15は閾値と基準値を版管理された設定・JSONへ固定し、usage欠落を不合格にし、1コマンドで連続2回を判定する。DV-18は送信をブロックするスタブを使い、送信中に永続カウンタがすでに増えていることを観測する。

確認できた閉鎖点は次の3点です。

- `check`をエージェント手から外したため、片側は手数6＋有効な途中確認1＝7手番、両側・最大2呼び出しで正常時28回、という算術は成立します。
- 交渉用ポリシーが追記のみで、矛盾するアンカーを拒否する限り、「受けられる」「受けられない」は変わらず、「本人確認が必要」だけが変わり得ます。C-43の規則自体は正しいです。
- 有効な`check`で連続無効手を戻さないこと自体は、FR-12・AC-06のポリシー非依存性を壊しません。問題はX-51のフィードバック消失です。

読み取り専用のため、台帳・state・レビュー成果物は更新していません。また今回はCodex側の批評で、design-loopが要求するfresh Fable/Claudeの保存済みレビューは生成していません。

