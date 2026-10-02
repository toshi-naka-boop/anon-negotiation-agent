v11 は現状のままでは批評通過にできません。新規欠陥は high 3 件、medium 2 件です。今回は Codex 側の読み取り専用批評で、ファイルは変更していません。

1. **X-45｜種別: 設計｜重大度: high｜24 回という構造上限を手数 6 から導けない**

   - **破綻シナリオ:** 有効な `ask_principal` と `check` は手数に数えません。また `Plan.checks` が空なら、通常の `Move` と同じ規則で `move=check` も出せます。このため、途中確認後に同じ側が再び計画する経路や、直接 `check` を繰り返す経路は「側ごと 6 手 × 2 回」に含まれません。それでも §8.2 は 24 回だけ予約し、実呼び出しが予約を超えないと断言しています。[design.md:274](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:274) [design.md:425](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:425) [design.md:509](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:509) [design.md:781](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:781)
   - **直し方の方向:** 「手数」と別に、計画開始ごとに必ず消費する永続的な LLM 手番カウンタを設けるか、`ask_principal`・直接 `check`・その後の応答手まで含む証明可能な上限を算出して予約してください。`Plan.move` から `check` を除くことも必要です。

2. **X-46｜種別: 設計｜重大度: high｜割り込み・再起動・再試行で 1 手番 2 回の上限が失われる**

   - **破綻シナリオ:** 計画後の `check` は金庫に残りますが、`Plan` と確かめの進捗は永続化されません。確かめ途中で一時停止・再開による 409 が起きる、または `web` が落ちると、同じ `to_move` のまま計画からやり直します。これを繰り返せば同じ手番で何度でも LLM を呼べます。さらに、Gemini が生成済みなのに A2A 応答だけ失われた場合、再試行は新しいセッションで再生成され得るため、「応答がない再試行は課金されない」という前提も成立しません。[design.md:495](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:495) [design.md:504](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:504) [design.md:507](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:507) [design.md:785](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:785)
   - **直し方の方向:** 金庫に `{turn_id, phase, plan, checked, llm_attempts}` を保存し、409・再起動後は生成済みの段階から再開してください。各物理的モデル呼び出しの前に予算を消費し、「モデルを呼ぶ前に失敗した」と確認できる場合だけ安全に再試行する必要があります。

3. **X-47｜種別: 設計｜重大度: high｜日付変更で未消化の予約が消え、1 日 1,500 回を超えられる**

   - **破綻シナリオ:** 23:59 に交渉を 62 件作成して 1,488 回分を予約し、その交渉が一時停止や障害で翌日に実行されるとします。0 時に枠が 0 へ戻るため、翌日さらに 62 件を予約でき、旧日分と新日分の呼び出しが同じ日に走ります。交渉寿命は 72 時間なので、複数日の予約を持ち越せます。[design.md:376](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:376) [design.md:782](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:782)
   - **直し方の方向:** 未消化の予約は日付変更で消さず、翌日の利用可能量を「1,500 − 持越し予約」にしてください。別案として、予約枠を「作成日の新規コミット上限」と呼び直し、実呼び出し用の別のローリング上限を設けます。DV-18 に日付をまたぐ進行中交渉を追加する必要があります。

4. **X-48｜種別: 設計｜重大度: medium｜計画内の確かめが通常手の副作用を持ち、評価配分と連続無効手を変える**

   - **破綻シナリオ:** 各計画が新しい案を 1 件確かめ、その後の決定だけが無効になる場合、通常の有効な `check` が先に連続無効手を 0 に戻すため、決定の無効手は毎回 1 にしかならず、3 回連続で停止しません。またレフェリーは提案用に 1 回しか評価を残しませんが、指示文は残り手数ぶんを残すよう求めています。正しい形の `Plan` でも、将来の提案ガード分を使い切れます。[design.md:419](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:419) [design.md:499](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:499) [design.md:574](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:574) [design.md:983](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:983)
   - **直し方の方向:** 計画内の確かめを通常の完成した `check` 手と区別し、評価は消費しても、最終 `Move` が確定するまで連続無効手をリセットしないようにします。確かめ可能数も `remaining_evaluations - remaining_moves` を上限としてレフェリー側で強制してください。

5. **X-49｜種別: 設計｜重大度: medium｜DV-15・17・18 は中核機構が無効でも合格できる**

   - **破綻シナリオ:** DV-17 は前文が 4,096 トークン以上かを見るだけで、計画・決定間の完全一致、実際のキャッシュ命中、`thinking_level` のモデル要求への反映を検査しません。DV-15 もキャッシュ済み・思考トークンを「記録」するだけなので、キャッシュ 0 件や思考設定の無視を失敗にしません。DV-18 は 24 を予約した事実だけを検査し、実呼び出しが予約以下か、409・再起動・再試行・日付持越しを検査しません。[design.md:560](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:560) [design.md:995](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:995) [design.md:997](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:997)
   - **直し方の方向:** 前文のハッシュ、実際のモデル要求の `thinking_level`、余分な文脈がないことを検査してください。キャッシュは「半数以上の命中」または明示キャッシュ ID の再利用を合格条件にします。DV-17 には各段階での停止・再起動・409、DV-18 には物理呼び出し数、再試行、日付またぎ、同時予約を追加し、DV-15 はモデル ID・設定ハッシュ・R-9 確定後の単価版を含む機械可読レポートを2回分残す形にします。

壁 2 と AC-05 については、上記とは別の新規破綻は見つけていません。文脈を固定前文＋`TurnInput` に限定する記述は維持され、`checked` も自分側の丸め済み3値だけです。また AC-05 は予算無制限でもグリッド1マスを保つ検査なので、呼び出し上限の欠陥だけで漏洩上限が細かくなる構造ではありません。[spec.md:277](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/spec.md:277)

