"""web: 画面 API・本人の確認・面談・レフェリー・見回り・攻撃モード・レート制限(design.md §1.1・§4.1・§6.3・§8)。

モジュールの構成。画面そのもの(静的な HTML/JS)は含まない。ここにあるのは、API とその裏の部品だけ。
- 組み立てと起動: app(FastAPI アプリの組み立てと起動)・services(部品の組み立て)・config(暫定値の読み込み)。
- 画面 API: api・api_models(本人の API とデモ用のエンドポイント。面談の送信は手順 9 の部分)・activity_api(活動ログ。FR-37)・
  panels_api(並べて見る画面の 2 つのパネル。FR-39)・meter_api(推定区間メーター。§8.3)・stages_api(段階開示の API)。
- 依頼者: session・session_middleware(匿名の依頼者のセッション)・principals_meta(利用記録)・locks(依頼者ごとのロック)・
  deletion(削除の流れ)・principal_sweeper(依頼者の見回り。30 日の自動削除)。
- 交渉の進行: vault_client(金庫のクライアント)・turn_input(TurnInput の組み立て)・referee(レフェリー。交渉ごとのタスクで LLM の呼び出しを担う)・
  sweeper(交渉の見回り)・stages(段階開示の状態と遷移)・ledger(開示台帳)・fictional_answerer(架空人物の自動応答。§4.4)。
- 面談: interview/(§5。API・進行・面談エージェント・途中の状態)。
- 攻撃モードと 3 枚の壁の実演: attack/(§8.1・§8.2)。
- 費用と入口の歯止め: llm_budget(LLM の物理の呼び出し数の計上と、作成の入場の制限)・limits(入口ごとのレート制限・SSE の同時本数・
  ログインなしの読み取りの枠)・client_ip(クライアント IP の取り方と、枠のキー。IPv6 は /64 単位)・body_limit(リクエスト本文の全体の上限)。
- 金庫・agents との通信: service_auth(サービス間の認証。台帳 X-37)・attested_transport(TEE 版の金庫への、検証してからピン留めする transport。§9)。
リプレイ(§8.4)の記録と再生は、このパッケージにはない(scripts/run_demo.py・scripts/replay_check.py)。

vault.api_models(金庫の API の型)と vault.clock(時計)は、同じ型を二重に書かないために
そのまま使う(自動回答は、フィクスチャの生の条件として vault.fixtures も使う)。vault.store(状態機械の実装)は直接使わない: 金庫の内側には HTTP でしか触れない。
"""
