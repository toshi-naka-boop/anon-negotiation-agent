v6 固有の欠陥を5件に絞りました。U-01〜U-14、P-1〜P-4、および新しい根拠のない解決済み論点は除外しています。

1. **種別: 設計／重大度: high**  
   **一行タイトル:** `control`・`expire` が正本イベントを上書きする  
   **破綻シナリオ:** `version` は手の操作でだけ進む一方、`pause`・`resume`・`expire` も `events/{version}` に書く設計です。[design.md:263](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:263) [design.md:287](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:287) 手の後に一時停止すると、既存イベントと同じ文書IDへ書いて失敗または上書きし、唯一の正本である活動ログ・リプレイ・最終表示が欠落します。  
   **直し方の方向:** 楽観ロック用の `move_version` と、全状態変更で必ず増える `event_revision` を分離する。少なくとも `control`・`expire` も内部で一意なイベント番号を採番し、move→pause→resume→expire の交錯テストを追加する。

2. **種別: 設計／重大度: high**  
   **一行タイトル:** 相手からの提案で評価上限を超えた場合の遷移が存在しない  
   **破綻シナリオ:** 候補者が `check` 9回＋`propose` で評価10回、求人側が受信評価1回＋`check` 8回＋`propose` で10回に到達すると、その提案の候補者側評価が11回目になります。いずれも手数上限内ですが、受け手の予算切れに対するエラー・停止・ロールバック規則がありません。[design.md:366](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:366) [design.md:376](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:376) 上限超過、処理不能、相手の非公開カウンタ漏洩のいずれかになります。`accept` の再評価も計数対象か不明です。  
   **直し方の方向:** 各手について両側の評価コストを定義し、トランザクション冒頭で必要枠を確保する。受信評価・accept再検査・最終判定を通常予算の外にするか、受信用の予約枠を分離し、境界値テストを追加する。

3. **種別: 実装／重大度: high**  
   **一行タイトル:** 本人削除後も live 交渉のイベントが孤児として残る  
   **破綻シナリオ:** 本物の候補者と架空求人の `live` 交渉にはTTLがなく、本人削除では「交渉の文書ごと消す」とだけ定めています。[design.md:415](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:415) [design.md:417](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:417) Firestoreでは親文書を削除してもサブコレクションは削除されないため、`events/*` の組み合わせや途中回答が残り続けます。[Cloud Firestore公式](https://firebase.google.com/docs/firestore/data-model)  
   **直し方の方向:** `events` をカーソル付きで明示的に全削除してから親を消す、再実行可能な削除手順を設計する。親だけ消して成功扱いにしない検証をDV-06へ追加する。

4. **種別: 設計／重大度: medium**  
   **一行タイトル:** 「外した軸」の途中回答は、FR-22とAC-07を同時に満たせない  
   **破綻シナリオ:** 外した軸についての回答は交渉用コピーにしか保存しないため、終了時に消えます。[design.md:534](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:534) 次の交渉で同じ組み合わせを聞くと再び「本人確認が必要」になりますが、specのFR-22と設計のAC-07は「以後は金庫が答える」「次の交渉でも使われる」と要求しています。[spec.md:87](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/spec.md:87) [design.md:834](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:834)  
   **直し方の方向:** 外した軸は「当該交渉内だけ有効」という明示的例外をspecとAC-07へ入れるか、次回にも保持する代わりにFR-05の秘匿方針を変更する。両方を暗黙に満たす実装はできない。

5. **種別: 設計／重大度: medium**  
   **一行タイトル:** 仕様外の発表者バイパスが、静的URL秘密という新しい攻撃面を増やす  
   **破綻シナリオ:** Secret Managerの秘密を含む管理URLは、ブラウザ履歴・アクセスログ・共有などから漏れ得ます。取得者は公開全体上限の対象外となる発表者セッションを作れ、LLMコストの全体防御を迂回できます。[design.md:685](/Users/toshixa/dev/tenshokuagent/design/anon-negotiation-agent/design.md:685)  
   **直し方の方向:** 最小構成なら発表者専用認証経路を削り、当日の設定変更とリプレイで対応する。残すなら静的URL秘密をやめ、短寿命・一回限りの認証と、全発表者をまとめた別の全体上限を設ける。

なお、`design-loop` 所定の fresh Claude 批評はCLIが未ログインのため実施できませんでした。上記はCodexによる批評で、ファイル変更は行っていません。

