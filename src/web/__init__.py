"""web: 画面 API・本人の確認・面談・レフェリー・見回り(design.md §1.1・§4.1・§6.3)。

1d-1 の範囲は、金庫のクライアント(vault_client)、TurnInput の組み立て(turn_input)、
レフェリー(referee)、交渉の見回り(sweeper)、段階開示の状態の作成(stages)。
1d-2 の範囲は、依頼者のセッション(session・session_middleware)、利用記録 principals_meta
(principals_meta)、削除の流れ(deletion)と依頼者の見回り(principal_sweeper)、依頼者ごとのロック
(locks)、画面の API(api・api_models。面談の送信は手順 9 の部分だけ、デモ用のエンドポイントを含む)、
アプリの組み立てと起動(services・app)。画面(静的な HTML/JS)・面談エージェント・段階開示の遷移・
攻撃モード・レート制限・リプレイ・メーターは後の段。
反証 1 巡目の修正(fix-1w)で、サービス間の認証(service_auth。台帳 X-37)を足した。
②-a で、架空人物の途中確認への自動回答(fictional_answerer。§4.4)を足した。
③-0(v14)で、レフェリーを 1 手番 最大 2 回の呼び出し(計画 → 確かめ → 決定。referee・turn_input)にし、LLM に送る前の物理の
呼び出し数の計上(llm_budget。交渉ごと・1 日)と、それに基づく作成の入場の制限・冪等キーの引き当て(api)を足した。

vault.api_models(金庫の API の型)と vault.clock(時計)は、同じ型を二重に書かないために
そのまま使う(自動回答は、フィクスチャの生の条件として vault.fixtures も使う)。vault.store(状態機械の実装)は直接使わない: 金庫の内側には HTTP でしか触れない。
"""
