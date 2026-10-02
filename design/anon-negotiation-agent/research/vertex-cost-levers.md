# Vertex AI コスト削減レバー調査（gemini-3.5-flash / global）

- 調査日: 2026-10-02。一次資料は公式ドキュメント本文と python-genai・ADK のソース。docs.cloud.google.com の本文は WebFetch では取れなかったため、ブラウザペインで本文を読み取った（読み取りのみ）。
- 名称: Vertex AI の docs は「Gemini Enterprise Agent Platform」に改称済み。URL は `docs.cloud.google.com/gemini-enterprise-agent-platform/...` に移り、旧 `vertex-ai/generative-ai/...` は 301 で転送される。
- 表記: **未確認** = 一次資料で確認できなかった事項。**解釈・推測** = 資料から私が導いた事項。
- 引用: 著作権への配慮で、原文の直接引用は §2 の 1 文のみ。ほかは要旨で、原文は各 URL の該当節で確認できる。
- docs の最終更新日（ページ末尾の表示）: caching overview・ZDR・thinking・structured output は 2026-10-01。GenerateContentResponse 参照は 2026-08-10、Schema 参照は 2026-05-12。価格ページは更新日の表示なし。

## 1. コンテキストキャッシュ（gemini-3.5-flash / Vertex AI）

| 項目 | 内容 |
|---|---|
| 対応 | gemini-3.5-flash は implicit・explicit どちらの対応モデル一覧にも載っている（3.5 Flash-Lite も同じ）。[overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview) |
| 最小トークン（Vertex） | implicit・explicit 共通の表で「Gemini 3 ファミリ = 4,096」。別行で「3.0 Flash Preview・3.1 Pro Preview・3.7 Flash・3.8 Flash は 6,144（implicit のみと注記）」。3.5 Flash の個別行はなく、**解釈**として 4,096。[overview の Limits 表](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview) |
| 最小トークン（参考） | Developer API 側の表は 3.5〜3.8 Flash・3.1 Pro Preview がすべて 4,096 で、Vertex の 6,144 と食い違う（別製品の記述）。[Gemini API caching](https://ai.google.dev/gemini-api/docs/caching)（2026-09-02 更新） |
| system_instruction | キャッシュ対象にできる（API 仕様上）。`CachedContent` の `systemInstruction`（text のみ）も `contents` も optional。公式 create サンプルは gemini-3.5-flash で `system_instruction` と `contents` を渡している。最小トークン未満では作れない前提（**解釈**）。[CachedContent](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1/projects.locations.cachedContents), [create](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-create) |
| 利用時の制約 | キャッシュ側に system_instruction / tools / tool_config を入れたら、リクエスト側では再指定しない。[use](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-use) |
| 割引 | implicit は 90% 引き。explicit も 2.5 以降は 90% 引きで、割引が確実。キャッシュ作成に使った入力は通常入力単価で課金（書込み割増なし）。implicit に保管料はなく、explicit は保管時間で課金。[overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview) |
| 単価 | cached 入力 $0.15 / 1M に対し通常入力 $1.50 / 1M（global, 200K 以下）。[pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing) |
| explicit 保管料 | 3.5 Flash は表記 $0.000001 / token / hour、つまり **$1.00 / 1M tokens / hour**（換算は私）。[pricing の Context Cache Storage 表](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing) |
| TTL | 既定 60 分、最小 1 分、最大は上限なし。`ttl` か `expire_time` で指定し、期限前なら更新できる。期限切れは再作成が必要。[create](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-create), [overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview) |
| エンドポイント | global で利用可。CMEK は global 非対応、シドニー（australia-southeast1）は非対応。[create](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-create) |
| ヒットの報告 | 応答の `usageMetadata.cachedContentTokenCount`（Python SDK は `usage_metadata.cached_content_token_count`）。implicit・explicit 共通。`promptTokenCount` はキャッシュ分を含む。[overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview), [REST UsageMetadata](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1/GenerateContentResponse), [SDK types](https://github.com/googleapis/python-genai/blob/main/google/genai/types.py) |
| implicit の保持時間 | 公式に数値の記載なし → **未確認**。公式の助言は「大きい共通部分を先頭に置く」「似た接頭辞のリクエストを短時間に送る」のみ。Google 社員のフォーラム回答は「implicit は TTL を指定できず、保証もない」旨（Developer API の話）。[overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview), [forum](https://discuss.ai.google.dev/t/have-anyone-checked-out-the-implicit-caching-for-gemini-api-caches-hits-are-inconsistent-for-me/82666) |
| 第三者の実測報告 | Developer API の 3 Flash Preview で、接頭辞が同一でも約 9K〜17K トークン帯で cached が 0 になる報告（open）。[python-genai #2064](https://github.com/googleapis/python-genai/issues/2064) tools を定義すると implicit が効かない報告（not planned で close）。[vercel/ai #11513](https://github.com/vercel/ai/issues/11513) いずれも Vertex の 3.5 Flash で再現するかは **未確認**。 |
| ADK との接続 | explicit は App レベルの `ContextCacheConfig`（`min_tokens` 既定 0、`ttl_seconds` 既定 1800、`cache_intervals` 既定 10、Gemini 2.0 以上）。`static_instruction` は固定指示を system instruction の先頭に固定し、可変の `instruction` をユーザー側へ移すが、それだけでは explicit は有効にならない。[ADK caching](https://adk.dev/context/caching/index), [llm_agent.py](https://github.com/google/adk-python/blob/main/src/google/adk/agents/llm_agent.py) |

固定指示 2,000 トークンだけでは 4,096 に届かず、implicit・explicit とも対象外になる（**解釈**）。履歴を追記型で積む設計なら、共通接頭辞（固定指示と過去ターン）が 4,096 を超えた呼び出しからヒットし得る。response schema が接頭辞に数えられるかは **未確認**（スキーマ自体は入力トークンに算入される: [structured output](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output)）。

## 2. データキャッシュ（プロジェクト単位）とコンテキストキャッシュの関係

- 設定の実体: Gemini は入力・出力・派生データをメモリ上のみ（ディスク保存なし）、プロジェクト単位で分離、24 時間 TTL でキャッシュするのが既定。ZDR には反しないと明記されている。プロジェクト単位で無効化でき、`cacheConfig` の `disableCache` を true にする PATCH（`roles/aiplatform.admin` が必要）。変更は全リージョンに適用。GET で現在値を確認でき、有効時は応答に `disableCache` が出ない。[ZDR の in-memory data caching 節と Enabling and disabling 節](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/zero-data-retention)
- 文書上の接点は caching overview の explicit 節の 1 か所のみ。原文: "To prevent cache data retention, disable implicit caching and avoid creating explicit caches."（同節）。この文の参照リンクの飛び先が、上記 ZDR の `cacheConfig` 節（`#enabling-disabling-caching`）。[overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview)
- implicit: 公式は implicit を止める手段としてプロジェクトの `cacheConfig` を指している。よって `disableCache: true` なら implicit は効かなくなる（90% 割引なし）と読むのが妥当（**解釈**）。「止まる」と明記した文は見つからず、実機でも **未確認**。
- explicit: 上の文は「explicit cache を作らない」ことを別の行動として求めている。`cacheConfig` が explicit の作成・利用を止めるとは書かれていない。`disableCache: true` の project で `caches.create` が通るか、通った cache が使えるかは **未確認**。explicit は TTL の間データを保管するので、保持ゼロの目的とは相反する。
- 緊張関係: ZDR ページは in-memory キャッシュを ZDR に反しないとし、overview はデータ保持を避けるなら implicit を無効化せよと述べる。どちらの基準で運用するかは方針判断。
- 参考（別製品）: Developer API の ZDR ページは、explicit（cached_content）は保管データが残るので避けるよう明記。[Gemini API ZDR](https://ai.google.dev/gemini-api/docs/zdr)
- **Durable Caching（未確認）**: 第三者の GitHub PR が「2026-09-24 付の Google メール」を根拠に、2026-10-15 から Gemini 3.x 以降でプロンプトを最大 24 時間ディスク保存する Durable Caching が既定オンになると主張。公式の ZDR・overview・リリースノート（2026-10-01 時点）に記載なし。同 PR は `retentionConfig: {retentionType: EPHEMERAL}` が v1/v1beta1 で 400 になり、`disableCache: true` が現状の唯一の制御だと報告。[SAR_dispatch_flow PR #67](https://github.com/SCCSSAR/SAR_dispatch_flow/pull/67), [release notes](https://docs.cloud.google.com/gemini-enterprise-agent-platform/release-notes)
- 診断: まず project の `cacheConfig` を GET して `disableCache` の有無を確認（手順は上記 ZDR ページ）。無効なら implicit のヒットは出ない前提で数値を見る。

## 3. 思考（thinking）制御

| モデル | 指定できる thinking_level | 既定 |
|---|---|---|
| gemini-3.5-flash | MINIMAL / LOW / MEDIUM / HIGH | MEDIUM |
| gemini-3.5-flash-lite | MINIMAL / LOW / MEDIUM / HIGH | MINIMAL |
| gemini-3.6-flash | MINIMAL / LOW / MEDIUM / HIGH | MEDIUM |
| gemini-3.7-flash / 3.8-flash（Cyber 含む） | LOW / MEDIUM / HIGH（MINIMAL なし） | MEDIUM |
| gemini-3.1-flash-lite | MINIMAL / LOW / MEDIUM / HIGH | MINIMAL |
| gemini-3-flash-preview | MINIMAL / LOW / MEDIUM / HIGH | HIGH |
| gemini-3.1-pro-preview | LOW / MEDIUM / HIGH（思考オフ不可） | HIGH |

出典: [Thinking](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking)

- `thinking_budget`: Gemini 3 で `thinking_level` と同一リクエストに併記するとエラー。3 未満のモデルに `thinking_level` を使うのもエラー。3.x で `thinking_budget` 単独が Vertex で有効かは Vertex docs に明記なし → **未確認**（Developer API は後方互換で有効と記載: [Gemini 3 guide](https://ai.google.dev/gemini-api/docs/gemini-3)）。[Thinking](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking)
- MINIMAL は「ほぼゼロ」で完全オフではない。Thinking ページは、MINIMAL でも thought signature が必要で、無いと 400 になると述べる。一方 signature ページは、必須なのは functionCall を含む応答の signature で、function calling なしの応答では省略しても失敗しないと述べる。ツールなしの構造化出力なら影響は小さいはず（**解釈**）。ツールを使うエージェントは応答パートを丸ごと履歴に戻す必要がある。[Thinking](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking), [signatures](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking/thought-signatures)
- 課金: 思考トークンは出力単価で課金（価格表の出力行が応答と推論をまとめて 1 つの単価で表示）。Developer API docs は、要約しか返らなくても生成した全思考トークンが課金対象と明記。[pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing), [Thinking](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking), [Gemini API thinking](https://ai.google.dev/gemini-api/docs/thinking)
- 報告: `usageMetadata.thoughtsTokenCount`（SDK は `usage_metadata.thoughts_token_count`）。`candidatesTokenCount` とは別枠で、`totalTokenCount` は prompt・candidates・toolUsePrompt・thoughts の合計。課金対象の出力 = candidates + thoughts は**解釈**。[REST UsageMetadata](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1/GenerateContentResponse), [SDK types](https://github.com/googleapis/python-genai/blob/main/google/genai/types.py)
- 各レベルの思考トークン数の公式数値: **未確認**。
- `max_output_tokens` が思考と出力の合算に効くという記述は Developer API docs のみ。Vertex の GenerationConfig 参照には記載なし → **未確認**。低く絞ると JSON が途中で切れる恐れがある。[Gemini API thinking](https://ai.google.dev/gemini-api/docs/thinking)
- Gemini 3.5 Flash 以降は前ターンの思考が既定で保持されるとの記述あり。マルチターンで入力トークンが増えるかは **未確認**。[signatures](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking/thought-signatures)
- SDK 要件: google-genai は v1.51.0 で `thinking_level`、v1.56.0 で MINIMAL / MEDIUM が追加。[python-genai CHANGELOG](https://github.com/googleapis/python-genai/blob/main/CHANGELOG.md)

ADK の LlmAgent への渡し方（公式サンプルの転載ではなく、型定義から組んだ最小例）:

```python
from google.genai import types
from google.adk.agents import LlmAgent

agent = LlmAgent(
    name="negotiator", model="gemini-3.5-flash", instruction=INSTRUCTION,
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
    ),
)
# 拒否される旧版 ADK では planner=BuiltInPlanner(thinking_config=types.ThinkingConfig(...)) を使う
```

- ADK 公式 docs の thinking 例は `thinking_budget` 版（BuiltInPlanner）のみ。`thinking_level` 版の公式例は **未確認**。[ADK planner docs](https://github.com/google/adk-python/blob/main/docs/guides/planners/planner/index.md), [ADK LLM agents](https://adk.dev/agents/llm-agents/index)
- `LlmAgent` の `generate_content_config` 検証は、v1.25.0・v2.6.1・main のソース（WebFetch で確認）で tools / system_instruction / response_schema / base_url を拒否するが、thinking_config は拒否しない。Issue #3905 が報告する旧版は拒否していた。planner と両方指定すると planner 側が優先。[llm_agent.py](https://github.com/google/adk-python/blob/main/src/google/adk/agents/llm_agent.py), [Issue #3905](https://github.com/google/adk-python/issues/3905), [ADK planner docs](https://github.com/google/adk-python/blob/main/docs/guides/planners/planner/index.md)
- ADK CHANGELOG: 2.8.0 で Gemini 3 の thought signature を履歴に保持し、思考要約を履歴として再送しないよう変更。CHANGELOG で確認できた最も新しい版は 2.10.0（2026-09-24）。[ADK CHANGELOG](https://github.com/google/adk-python/blob/main/CHANGELOG.md)

## 4. 価格（Vertex AI, global, Standard PayGo, USD / 1M tokens, 入力 200K 以下）

確認日 2026-10-02。ページ: [Agent Platform Pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)

| モデル | 入力 | cached 入力 | 出力（思考込み） | explicit 保管 |
|---|---|---|---|---|
| gemini-3.5-flash | $1.50 | $0.15 | $9.00 | $1.00 / 1M tok / h |
| gemini-3.5-flash-lite | $0.30 | $0.03 | $2.50 | $1.00 / 1M tok / h |

- 入力 200K 超でも同額。非 global は +10%（3.5 Flash: $1.65 / $0.165 / $9.90）。注記に 50% クレジット還元の販促記述があるが条件は未確認。[pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)
- Priority は 3.5 Flash で $2.70 / $0.27 / $16.20。Flex/Batch は $0.75 / $0.075 / $4.50（半額）。[pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)
- Flex PayGo は Preview。標準の 50% 引き、global のみ、レイテンシ長め・スロットル強め、タイムアウト最大 30 分、専用ヘッダで指定。3.5 Flash 対応。[Flex PayGo](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/flex-paygo)
- 参考: 3.8 / 3.7 / 3.6 Flash は 2026-12-31 まで導入価格 $0.75 / $0.075 / $3.75、2027-01-01 以降は $1.50 / $0.15 / $7.50。3.1 Flash-Lite は $0.25 / $0.025 / $1.50。[pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)
- 200 以外の応答（4xx/5xx）は入出力とも課金されない。200 で返った分は課金される。[pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)

## 5. 構造化出力（anyOf / oneOf / nullable / enum）

- ガイドが挙げる対応フィールド（response_schema）: anyOf・enum（文字列のみ）・format・items・minimum/maximum・minItems/maxItems・nullable・properties・description・propertyOrdering・required。未対応フィールドはエラーにならず無視される。対応モデルに 3.5 Flash・3.5 Flash-Lite を含む。[structured output](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output)
- `anyOf` と `nullable` は対応（モデル別の差の記載なし）。`oneOf` は一覧になく、Vertex の `Schema` 型にも無いので `anyOf` を使う。[structured output](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output), [Schema](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/Shared.Types/Schema)
- enum は文字列のみ。ご提示のエラー（enum を持つスキーマの type は STRING）と整合する。整数の選択肢は STRING + `["1","2"]` にして後で int 変換するか、INTEGER + minimum/maximum で表す（**推測**）。Schema 参照には INTEGER + `format: enum` + 文字列値の例があるが、ガイドの記述と実際の 400 に合わないので頼らない。[structured output](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output), [Schema](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/Shared.Types/Schema)
- `response_json_schema`: SDK の docstring は `$defs`/`$ref`・`anyOf`・`oneOf`（anyOf と同じ扱い）・enum（文字列と数値）・`additionalProperties`・`prefixItems` などに対応と記載。循環参照は限定的。v1.74.0 で Agent Platform 向けの `oneOf` 対応が追加された。Vertex 実機での数値 enum・`oneOf` の動作は **未確認**。[SDK types](https://github.com/googleapis/python-genai/blob/main/google/genai/types.py), [SDK CHANGELOG](https://github.com/googleapis/python-genai/blob/main/CHANGELOG.md)
- 注意: Vertex の REST 参照では `responseSchema` と `responseJsonSchema` はどちらも deprecated で、後継は `responseFormat[].text.schema`（JSON Schema）。ただしガイドと SDK は引き続き response_schema を案内している。[GenerationConfig](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1/projects.locations.tuningJobs#GenerationConfig), [structured output](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output)
- 複雑なスキーマは 400（InvalidArgument）になり得る。対策はプロパティ名・enum 名を短く、optional と enum の値数を減らし、配列のネストを平坦にすること。[structured output](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output)
- anyOf に `{"type":"null"}` を並べる書き方の Vertex での扱い: **未確認**（`nullable: true` のほうが公式ガイドに沿う）。

## 6. 補足

- 環境変数: google-genai の README は `GOOGLE_GENAI_USE_ENTERPRISE` を案内するが、`GOOGLE_GENAI_USE_VERTEXAI` もソース上は現役。両方を矛盾した値で設定すると ENTERPRISE が優先され警告が出る。[_api_client.py](https://github.com/googleapis/python-genai/blob/main/google/genai/_api_client.py)
- Developer API の Gemini 3 ガイドは、temperature を既定の 1.0 から下げるとループや性能低下の恐れがあると注意している（Vertex 側の同等記述は未確認）。[Gemini 3 guide](https://ai.google.dev/gemini-api/docs/gemini-3)

## 7. 設計への示唆

1. 最大のレバーは思考と出力。出力は入力の 6 倍、cached は入力の 1/10。2,000 トークンの固定指示を 100% キャッシュできても節約は約 $0.0027 / 呼び出しで、出力（思考込み）が約 300 トークン減れば同額（単価からの試算）。まず `thinking_level` を LOW（足りれば MINIMAL）にし、`thoughts_token_count` を前後で計測する。
2. 固定指示 2,000 トークンは 4,096 未満で単体ではキャッシュ対象外（解釈）。水増しして 4,096 にする案は、ヒット率が約 57% を超えないと赤字（試算: hit で −$0.0024、miss で +$0.0031 / 呼び出し）。履歴追記型なら自然に超えるので、固定部を先頭・可変部を後ろに保ち、`cached_content_token_count` を実測してから判断する。ADK で explicit を使うなら `min_tokens`（既定 0）を 4,096 以上にする（推測）。explicit は保管データが残るため方針との整合が要る。
3. プロジェクトの `cacheConfig` を GET して現状を確認する。`disableCache: true` なら implicit は効かない前提で設計する（文書上の対応付けで、実機検証が必要）。Durable Caching の報告（未確認）も踏まえ、運用方針（保持ゼロかコストか）を先に決める。
4. モデル選択肢: 3.5 Flash-Lite は入力 1/5・出力約 1/3.6・既定 MINIMAL。3.8 Flash は導入価格（〜2026-12-31）が入力 $0.75・出力 $3.75 と 3.5 Flash より安いが、MINIMAL がなく implicit の最小が 6,144。品質は eval-loop で比較してから切り替える。
5. スキーマと依存: enum は STRING のみ、`oneOf` は避けて `anyOf`、nullable は `nullable: true`。使用中の ADK（generate_content_config に thinking_config を置ける版か）と google-genai（MINIMAL は v1.56.0 以降）を `pip show` で確認する。
