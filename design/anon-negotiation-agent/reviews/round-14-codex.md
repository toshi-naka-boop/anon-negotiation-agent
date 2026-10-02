## 批評 14 巡目（最終）

新しい根拠があるものに限定すると、5件です。high 1件、medium 4件です。

### X-55

- 種別: 実装
- 重大度: high
- 一行タイトル: 自動再試行の無効化が実装・DVとも保証できていない
- 破綻シナリオ: v13は「1計上＝1 Vertex API request」を前提にしていますが、現実装はモデル名と生成設定だけを `LlmAgent` に渡しており、HTTP retry設定がありません（[llm_agents.py:56](/Users/toshixa/dev/tenshokuagent/src/agents/llm_agents.py:56)）。Google Gen AI SDKの `HttpRetryOptions.attempts` は未指定時5回が既定で、0または1が再試行なしです（[公式APIリファレンス](https://googleapis.github.io/python-genai/genai.html)）。DV-17は `BaseLlm` スタブへの呼出回数しか数えないため、その下のHTTP層が429/5xxを再試行しても合格します。結果として、カウンタ上1回の手が最大5リクエスト・複数回課金となり、1日上限と交渉単位上限が実効的に破れます。
- 直し方の方向: 使用する実際のgoogle-genai/ADK経路で `HttpRetryOptions(attempts=1)` 相当を明示し、起動時にも値を検証してください。DV-17には、429・503を返す偽HTTP transportを置いて「送信されたHTTPリクエストそのものが1回」を測る試験を追加します。

### X-56

- 種別: 設計
- 重大度: medium
- 一行タイトル: 429・5xxの計上を戻す規則では、金額のハード上限にならない
- 破綻シナリオ: [design.md:808](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:808)は送信前に加算した後、429・5xxなら減算します。しかし、エラー応答でも上流で推論処理が始まっていた場合や、プロキシが処理済み応答を偽の502へ置き換えた場合、そのリクエストが課金対象でも枠が戻ります。これを繰り返すと、カウンタは1500以下のまま実費が `$55` を超えます。また、日付跨ぎの最大41交渉分を認めながら、最悪金額は1500回だけで算出されています。なお「減算前に落ちる」ケースは過大計上になるため、安全側です。
- 直し方の方向: 安全上限用のattemptカウンタは429・5xxでも減算しないでください。成功数や推定課金数が必要なら別カウンタに分離します。日次金額は、前日からの持越し送信を含む全リクエストを同じ固定窓／rolling 24hで拘束するか、持越し最大数を最悪金額へ明記して上限値を引き直します。DV-18には「処理済みだが502」「減算直前のクラッシュ」「日付跨ぎ」を入れるべきです。

### X-57

- 種別: 設計
- 重大度: medium
- 一行タイトル: 作成冪等性がVault成功後・Web対応表保存前のクラッシュで途切れる
- 破綻シナリオ: v13はWeb側 `stages/{nid}` に `request_id → negotiation_id` を保存するとしています（[design.md:812](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:812)）。しかし処理順はVault作成後にWeb状態を登録する形で（[api.py:163](/Users/toshixa/dev/tenshokuagent/src/web/api.py:163)）、両者を原子的に確定できません。Vault作成成功直後にWebが落ちると、再起動後の同一キー要求は既存交渉を発見できず、起動時503や満杯判定を先に受けます。「同じキーなら同じ交渉を返す」という作成冪等性を満たしません。
- 直し方の方向: `request_id` の正本をVaultに置き、create前に「このキーの既存交渉」を検索できるAPIを設けてください。既知キーの再送は起動時503・新規受付上限より先に返します。Vault commit直後、Web保存直前へ障害を注入するDV-18試験も必要です。

### X-58

- 種別: 設計
- 重大度: medium
- 一行タイトル: `usage` のA2A artifact metadata経路に受信スキーマと検証規則がない
- 破綻シナリオ: v13はusageをartifact metadataで返すとしますが（[design.md:615](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:615)）、`Usage` のStrictModel、許容キー、値域、モデル識別子、artifact全体の形を定義していません。現受信側は全artifactのpartsを平坦化してDataPartを1個探すだけで、artifact metadataを検査しません（[client.py:68](/Users/toshixa/dev/tenshokuagent/src/agents/client.py:68)）。したがってusageの欠落・負値・別モデル値・余分な自由文を受理し、DV-15の費用判定を過少申告で通せます。X-41で閉じた「A2A受信値を厳格検証する」境界がusage経路には延長されていません。
- 直し方の方向: `Usage(StrictModel)` と厳格な応答envelopeを定義し、completed Taskが1個、artifactが1個、DataPartが1個、metadataはusageだけ、token数は非負かつ設定上限内、モデルIDも設定値と一致、未知フィールド禁止、としてください。DV-15/17で欠落・負値・余分なartifact・異なるモデルIDを拒否する試験を追加します。

### X-59

- 種別: 設計
- 重大度: medium
- 一行タイトル: 新しい評価予約条件では、HITL未発生でも評価1回が恒常的に余る
- 破綻シナリオ: [design.md:522](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:522)の条件は `残り評価 > 残り手数 + 残り途中確認` です。初期値が評価16、手6、途中確認1なら、評価チェックを行えるのは残り評価が7になるまでの9回です。途中確認が一度も発生しない交渉でも最後の1回を解放できず、§3.5の「6手＋評価10回」という算定と食い違います。境界付近の候補では、使用可能な評価が残ったまま合意判断へ届かず終了し、旧条件で得たDV-14の34/36到達率を維持できない可能性があります。
- 直し方の方向: 最も単純には評価上限を17へ増やし、日次予約量も170へ更新します。別案として途中確認枠が不要と確定した時点で予約を解放しますが、その確定条件を状態機械として明文化する必要があります。いずれの場合も、v13の正確なreferee条件でDV-14の36初期点を再実行してください。

確認の結果、次の直しは閉じています。

- `last_error`・`last_invalid` をエージェントの手だけで生成・消費し、途中の評価チェックで失わない規則
- `control{stop_cost_limit}` から `paused/awaiting_principal` へ遷移し、再送を409にする経路
- 「本人確認が必要」の再確認を、後続の本人回答がある場合だけ行う規則。アンカー集合は単調増加で、古いスナップショットが本人回答なしに変化しないため整合しています
- Planを全体検証してからchecksを優先する読み方自体は、move単独の不正値を実行へ漏らしていません
- `max_output_tokens` が思考tokenも含む点は公式仕様と一致しています（[Gemini thinking guide](https://ai.google.dev/gemini-api/docs/generate-content/thinking?hl=en)）。ただしX-55のとおり、実際の生成設定とHTTP再試行設定をDVで下層まで確認する必要があります

