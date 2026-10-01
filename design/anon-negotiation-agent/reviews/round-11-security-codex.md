> critic: codex/gpt-5.6-sol reasoning effort xhigh（534 秒。ログは round-11-security-codex.log）— 反証 3 巡目（安全性だけ。上限の最後）。新しい指摘の ID は台帳で X-44 を振る

## 判定

- **X-39 — 閉じた。** 32バイト以上に加え、異なるバイト値が16種類未満の鍵を拒否します。[session.py:75](/Users/toshixa/dev/tenshokuagent/src/web/session.py:75) 乱数性を完全には証明できない点と本番生成手順は、台帳どおりデプロイ時の確認事項として蒸し返していません。
- **X-40 — 閉じた。** 金庫に本番起動口が追加され、web・金庫の両方が `negotiation_core` の共通マスク処理を呼びます。[vault/app.py:167](/Users/toshixa/dev/tenshokuagent/src/vault/app.py:167) [log_privacy.py:34](/Users/toshixa/dev/tenshokuagent/src/negotiation_core/log_privacy.py:34) ただし `log_privacy.py` は現在未追跡なので、コミットから落とさないことが条件です。
- **X-41 — 閉じた。** DataPart の metadata、非空の filename、許可外の mediaType をLLM起動前に拒否します。[validation.py:123](/Users/toshixa/dev/tenshokuagent/src/agents/validation.py:123) 空の metadata・filename は「指定なし」と同じ扱いです。
- **X-42 — まだ残る。** `aud` の完全一致と有限・未来の `exp` は検査され、401・403後の再送も同じメソッド・URL・本文で1回だけです。[service_auth.py:83](/Users/toshixa/dev/tenshokuagent/src/web/service_auth.py:83) ただし、下記のとおり2回目に拒否されたトークンがキャッシュに残ります。
- **X-43 — 閉じた。** metadataとPydanticの余分な項目名は、実名を返さず `<unknown>` と件数に集約されます。[validation.py:77](/Users/toshixa/dev/tenshokuagent/src/agents/validation.py:77)

参考として、**L10-1も閉じています**。404・409以外の4xx後は、2秒ではなく見回り間隔の60秒待ちます。[referee.py:181](/Users/toshixa/dev/tenshokuagent/src/web/referee.py:181)

## 新しく入った欠陥

### 1. 実装 / low — 2回目に拒否されたトークンをキャッシュから捨てない

- **破綻シナリオ:** `POST /v1/negotiations/0123456789abcdef/moves` に `{"expected_version":7,"side":"candidate","move":"accept"}` を送る。キャッシュ中のトークンT1が401になり、実装はT1を破棄してT2を取得し、同じリクエストを1回再送する。T2も403になった場合、再送は正しく終了するが、T2はキャッシュに残る。次の内部リクエストは、既に拒否済みのT2をもう一度送ってから取得し直すため、不要な拒否リクエストと認証取得が繰り返される。agents側では外側の再試行も重なり、要求数を増幅する。
- **該当ファイルと行:** [service_auth.py:187](/Users/toshixa/dev/tenshokuagent/src/web/service_auth.py:187)。最初の応答だけを受け取り、再送後の応答を受け取って破棄する処理がありません。
- **直し方の方向:** 2回目の `yield` の応答も受け取り、それも401・403ならキャッシュだけを破棄して終了する。3回目は送らない。並行要求で新しい正常トークンを消さないよう、「拒否されたトークンが現在のキャッシュと同じ場合だけ削除」にするとより安全です。

この1件以外に、今回の修正によって新しく入った安全性の欠陥は見つかりませんでした。テストは実行せず、秘密領域も参照していません。

