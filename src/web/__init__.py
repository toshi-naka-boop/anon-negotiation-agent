"""web: 画面 API・本人の確認・面談・レフェリー・見回り(design.md §1.1・§4.1)。

1d-1 の範囲は、金庫のクライアント(vault_client)、TurnInput の組み立て(turn_input)、
レフェリー(referee)、交渉の見回り(sweeper)、段階開示の状態の作成(stages)。
依頼者セッション・利用記録・削除の流れ・画面の API は 1d-2。

vault.api_models(金庫の API の型)と vault.clock(時計)は、同じ型を二重に書かないために
そのまま使う。vault.store(状態機械の実装)は直接使わない: 金庫の内側には HTTP でしか触れない。
"""
