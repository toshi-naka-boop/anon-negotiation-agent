新規欠陥は5件です。U-01〜U-14、P-1〜P-4、解決済み索引の論点自体は再提起していません。

1. **種別:** 設計  
   **重大度:** high  
   **一行タイトル:** `accept` と `judge` の分離により、成立済み交渉を取消でき、コピーも終了後に残る  
   **破綻シナリオ:** `accept` は `status=agreed` にするだけで、結果確定・コピー削除は後続の `judge` に委ねています。一方、`cancel` は `judged` 以外から可能です。そのため accept 直後に取消が競合すると、同じ成立済み交渉が「高/中」にも「なし」にもなり得ます。また、web停止中はコピーが残り、FR-14・AC-17に直接違反します。[design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:339) [spec.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/spec.md:76)  
   **直し方の方向:** `accept` のトランザクション内で最終判定、`result`、`judged`、コピー・複製削除、最終イベントまで確定する。独立した `agreed` 状態と `judge` APIは削除でき、過剰実装も減らせます。

2. **種別:** 設計  
   **重大度:** high  
   **一行タイトル:** 金庫で判明する無効手が記録経路から抜け、回数制限と自己修正が壊れる  
   **破綻シナリオ:** レフェリーが `invalid` を登録するのはスキーマ違反・タイムアウト・A2Aエラーだけです。しかし `propose` の自分側不受理、pending offerなしの `accept`、適用不能な `ask_principal` は、スキーマ検証後に金庫で初めて無効と分かります。この応答を `invalid` として原子的に記録する経路がなく、`last_error` が次手に渡らず、temperature 0 のエージェントが同じ手を繰り返します。評価済みなのにカウンタを消費しない実装ならFR-11も迂回されます。[design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:354) [design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:431)  
   **直し方の方向:** `/moves` 自身が、ドメイン上の無効手を同一トランザクションで「評価回数消費・無効回数・version・イベント・last_error」まで記録する。競合や古いversionだけは無消費の409に分離します。

3. **種別:** 設計  
   **重大度:** medium  
   **一行タイトル:** 「一覧の全件」に永続キューと作成冪等性がなく、再起動・再送で欠落または重複する  
   **破綻シナリオ:** 残り求人は単に「webの待ち行列」とされ、永続データ項目やカーソルがありません。web再起動後に復元できるのは既に作成済みの交渉だけなので、残件が消えます。また `POST /negotiations` に冪等キーがなく、同時要求や応答喪失後の再送で、単一進行中制約を破るか、デモ用複製を二重作成します。[design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:316) [design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:577)  
   **直し方の方向:** 候補者単位の永続ジョブに対象求人、cursor、`active_nid`、request IDを持たせる。交渉作成・予算予約・active lease更新を一つのトランザクションにし、同じrequest IDには既存nidを返します。

4. **種別:** 設計  
   **重大度:** medium  
   **一行タイトル:** 最終イベント確定後の段階開示に、クラッシュ後の再発見経路がない  
   **破綻シナリオ:** 最終結果の正本はvaultイベントですが、webは写しを持たず、再開用一覧は`judged`を返しません。judge成功後、`stages/{nid}`の準備や画面通知より前にwebが落ちると、再起動後にそのnidを再発見できず、段0の自動表示、段1操作、活動ログ全件表示が欠落します。[design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:294) [design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:317) [design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:584)  
   **直し方の方向:** stage文書を交渉作成時に冪等作成するか、参加者別に未反映の終端交渉を列挙できるreconcile API／索引を設けます。最終確定直後でwebを落とす復旧テストも追加します。

5. **種別:** 設計  
   **重大度:** medium  
   **一行タイトル:** 側別に伏せた操作が、共有`version`の欠番から相手に見える  
   **破綻シナリオ:** イベントは全操作で共有versionを進め、相手側payloadだけをnullにします。相手が`after_version`で読めば、次の可視イベントが飛び番になり、隠した`check`、途中確認、pause/resumeの回数を推測できます。特に提案直後の2件の欠番から「本人確認が必要だった」と判別でき、側別viewの秘匿意図を破ります。[design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:261) [design.md](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:283)  
   **直し方の方向:** グローバルversionは金庫内部の競合制御専用にし、外向けeventsには側別の連番または不透明カーソルを発行します。相手側がnullの操作は、番号・件数も観測できないことをDV-10へ追加します。

