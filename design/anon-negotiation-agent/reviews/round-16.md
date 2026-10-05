critic: claude/claude-opus-5-5

## Round 16 — 2026-10-03 — 批評（設計書 v15 の下書き）

- 読んだもの: design.md（v15）、ledger.md（d865209 の時点。codex の X-65〜X-69 を含む）、research/tee-spike-contract.md（§15 の追記を含む）、research/tee-spike.md（点 2・3・4・6、手順 A〜F）。L16-1・L16-2 の裏取りに、src/web/referee.py・src/web/turn_input.py・src/vault/models.py・src/negotiation_core/schema.py・src/agents/instructions/candidate.md を読んだ。
- codex の X-65〜X-69 と重なるもの（L0 の主張の強さ、WIF・KMS の IAM の検査、participants の封印、single-flight、DV-17 の JSON モード、last_check の二重の意味）は出さない。下の C-56・C-57 は、X-65 の直し（deploy_check での完全一致の検査、主張を弱める）を入れても残る穴に絞った。

### 指摘

- [C-56] 種別: 設計 / 深刻度: high
  - 指摘: debug の段でも本番と同じ KEK（同じ鍵の版）を使い、「debug の DEK は `_tee/dek` を消して作り直す」で済ませている。debug の段に作られた暗号文は、本番に切り替えた後も本番の金庫で開けるので、運営者は自分が知っている DEK を本番に持ち込める。§9 の「鍵は本番イメージにしか渡らない（鍵の IAM を変えない限り）」が、IAM を一度も変えずに破れる。
  - 破綻シナリオ:
    1. 手順 A でプロバイダはテスト用の条件（`swname` だけ）で作られる。手順 B で digest D の principalSet に `cryptoKeyEncrypterDecrypter` が付く。手順 C の debug の VM（同じ digest D）で、金庫は `_tee/dek`（DEK_d を KEK の版 1 で包んだもの）を作る。運営者は SSH の root で DEK_d を見られる（§9 も認めている）。運営者はこのとき `_tee/dek` の文書を手元に写しておく（Firestore を読むだけ）。あるいは debug の VM で STS を通して KMS の encrypt を呼び、自分で選んだ DEK_x を包んだ暗号文を作っておく。
    2. 手順 D でプロバイダを本番の条件に更新し、`scripts/tee_reset_dek.py --yes` で `_tee/dek` を消す（§9 の「言えないこと」の手順どおり）。
    3. 本番の VM が起動する前に（起動した後なら VM をリセットして）、運営者は写しておいた `_tee/dek` を書き戻す。契約 §5 の 4 は「あれば wrapped_dek を decrypt する」で、誰が作った文書かを確かめない（`image_digest`・`created_at` は未検証の自己申告）。暗号文は同じ鍵の同じ版なので、本番の条件を通った金庫の decrypt は成功する。金庫はこれ以降、live のデータを DEK_d（または DEK_x）で封印する。
    4. 本番に切り替えた後、鍵の IAM も WIF の条件も一度も変わっていない。Admin Activity の監査ログに残るのは、計画どおりの切り替えだけ。Firestore の書き込みと KMS の decrypt は Data Access のログで、既定では無効。運営者は、§9 が「封印が守る」とした Firestore の直接の読み取り・エクスポート・バックアップ（有効なら PITR も。本人が削除した後のものを含む）を、いつでも復号できる。
    - 実害の増分: 運営者は読み出し口からも読める（§9 の「言えないこと」）。そのため増えるのは、削除した後のバックアップなどを復号できることと、公開する主張が偽りになること。ただし、§9 が debug について用意した手当て（DEK の作り直し）が効かないので、high とした。
    - あわせて: テスト用の条件には `project_id` も SA も入っていない。そのため debug の段の間は、どのプロジェクトの Confidential Space でも digest D を動かせば、このプロジェクトの KEK を使える。いまは Artifact Registry が非公開なので第三者は D を動かせない。しかし X-65 の直しとして、イメージを公開して第三者に中身を確かめさせる案を採ると、この段がそのまま穴になる。
    - 「鍵の IAM を変えない限り」の但し書きも足りない。鍵の IAM はそのままでも、WIF のプロバイダの条件を一時的に緩めれば、debug の VM が鍵を受け取れる。
  - 提案:
    - 手順 D で切り替えた直後に、KEK の新しい版を作って primary にし、debug の段にあった版を無効化して破棄を予約する。KMS の暗号文には版が入っているので、debug の段の暗号文は二度と開けない。そのあとで `_tee/dek` を作り直す（本番用に鍵を新しく作るのでもよい）。金庫は decrypt の応答の `usedPrimary` が真であることも確かめ、古い版の暗号文を拒む（版を primary に戻す操作は Admin Activity に残る）。
    - テスト用の条件で外すのは `dbgstat`・`STABLE` だけにし、`project_id`・SA・`hwmodel` は本番と同じにする。
    - §9 の「言えること」の但し書きを「鍵の IAM・WIF のプロバイダの条件・鍵の版を、本番に切り替えた後に変えない限り（どれも Admin Activity の監査ログに残る）」に直す。「言えないこと」の debug の項目は「DEK を作り直すだけでなく、鍵の版も替える」に直す。X-65 の deploy_check に「有効な鍵の版は、どれも本番の条件に切り替えた後に作ったもの」を足す。

- [C-57] 種別: 設計 / 深刻度: medium
  - 指摘: 「失効」の扱いがない。許可リスト `deploy/vault-releases.json` には追記しかできない。web はピン留めした後に検証し直さない。そのため、許可を取り消したい digest や、起動後に条件を満たさなくなった金庫にも、データを送り続ける。
  - 破綻シナリオ:
    1. 10/3〜4 のスパイクで記録する版 R0 は、`store.py` に封印を組み込む作業（10/5 以降）より前のコードなので、live のデータを平文で書く。R0 は probe の検証のために許可リストに入る（契約 §12）。`tee_record_release.py` は追記だけで（契約 §13）、外す操作も、外す規則もない。KMS の R0 の principalSet も、研究報告の手順 F（片付け）でしか外さない。審査の間に VM を R0 で作り直すと、R0 は本番の条件で鍵を受け取り、web の検証も画面の表示（verified と、R0 のコミットへのリンク）も通る。手順 D の「`@sha256` の参照が通らなければタグ参照に戻す」で、作り直すイメージを取り違えた場合も同じ。それ以降、live の依頼者のデータは封印されないまま Firestore に書かれる。
    2. 起動済みの金庫が、後から条件を満たさなくなる場合。Confidential Space のイメージが失効すると `swname` が `GCE` になり（研究報告 3-3）、サポートが終わると `STABLE` が消える。web が検証し直すのは接続エラーのときだけ（契約 §8 の 5）。金庫は同じ証明書のまま数週間動ける（有効期間 90 日）ので、web はピン留めした金庫に送り続け、金庫は起動時に受け取った DEK を持ち続ける。`/api/tee/attestation` は verified=false を返すが、`attest()` が失敗したときにピンを捨てるとは、契約 §8 にも設計書 §9 にも書かれていない。画面は「検証に失敗」を出し、データの経路は「検証済み」のまま動き続ける。
    - 研究報告 2-5 には「イメージが失効しても、起動済みの金庫は動き続ける」とあるが、設計書 §9 には入っていない。
    - KMS の古い principalSet の消し忘れは、X-65 と同じ根。ここで足すのは、許可リストの寿命と、接続した後の検証し直し。
  - 提案:
    - 許可リストは「いま支持するリリースだけ」と決める。`tee_record_release.py` に置き換えの操作（例: `--supersede`）を足し、同じ手順で KMS の古い principalSet も外す（X-65 の deploy_check の完全一致は、この表を基準にする）。封印を組み込んだ版を出したら、R0 を外す。
    - web は既存の `attest()` を定期的に回す（例: 5 分ごと。発行枠は single-flight の 1 回）。方針の理由（`swname`・`support_attributes`・`debug`・`image_digest`・`override`・`project`・`service_account`）で落ちたらピンを捨て、閉じる側に倒す（429・503 は除く）。
    - 「言えないこと」に「検証は接続した時点のもの。失効は、次の検証（最大 5 分後）か金庫の再起動まで効かない」と書く。

### low のメモ（件数に数えない）

- L16-1（設計 / 観点 c）: JSON モードにしたため、C-51 の寛容な読み（`checks` と `move` の両方があれば `move` を無視する）が、`move` の側が不正なときに効かない。`Plan` は strict に検証する（`move: AgentMoveType`）。そのため、`{"checks":[A,B],"move":"check"}` や、`checks` は正しく `package` だけがグリッド外の計画は、計画全体が `schema_invalid` になり、`last_invalid` は 3 つとも null になる（手がかりがない）。v14 では、応答スキーマの列挙がこの形を出させなかった。TurnInput の history には、レフェリーの確かめが `by: self, move: check` で並ぶ（§2.7・turn_input.py）ので、`move: "check"` が誘われうる。L15-2 で own_move_number から確かめを外したことと、history の見せ方も揃っていない。提案: `checks` が空でなければ、`move`・`package` を捨ててから検証する。history の確かめは別の値（例: `move: "checked"` か `by: referee`）にする。DV-17 に「正しい checks ＋ 不正な move」の場合を足す。DV-15 の実測で無効手が 0 件なので low とした。
- L16-2（設計 / 観点 b・c）: §2.7 の v15 の文は「グリッド外は `off_grid` の無効手」とする。しかし、金庫が `move=invalid` で受け付ける理由は `schema_invalid`・`agent_timeout`・`output_truncated` だけで（`vault.models.RegisteredInvalidReason`）、§3.5 の表と §4.1 の 6 もそう書いている。③ で §2.7 のとおりに `off_grid` を登録すると、金庫は 422 を返す。レフェリーはこれを「送り直しても直らない 4xx」として 60 秒待って読み直し、そのたびに計画を呼び直す。登録されないので、手数も連続無効手も進まない。結果は、手番の期限での timeout か、44 回の上限での stopped_cost になる。いまの実装は `schema_invalid` にまとめているので安全。§2.7 を「グリッド外も `schema_invalid`」に直すのが最小の手当て。
- L16-3（設計 / 観点 c。X-69 の補足）: L15-1 の置き換えでは、回答の前の `last_check`（Q）を評価し直した結果が捨てられる。後の計画が Q を並べると、history の Q は回答より前の「本人確認が必要」なので、C-48 の規則で確かめ直しになり、評価を 1 回使う。v14 なら view から無料で埋まっていた評価。X-69 を直すときに、Q の評価し直しも view に残す形（例: `last_check` と `last_answer` を分けて持つ）にすれば、この消費も避けられる。
- L16-4（設計 / 観点 b）: TEE を実施するときの構成に、§10 と AC-22 が追いついていない。AC-22 の「3 サービスの `/healthz` が 200」は、`deploy_check.sh` からは通らない。金庫には外部 IP がなく、IAP の規則は検証の後に消し、`/v1/attestation` 以外の経路はすべて web の SA のトークンが要る（契約 §9）ため。§10 の「金庫の本番の起動は `uvicorn vault.app:create_app_from_env`」と「Cloud Run IAM: vault の起動元」も、TEE 版（`python -m vault.tee.main`、VM）には当てはまらない。§3.3 の見出しの「呼べるのは web だけ」も、認証なしの `/v1/attestation` と合わない。提案: TEE のときの AC-22 は、「web の `/healthz` が金庫まで往復する」と「`/api/tee/attestation` が verified」の 2 つにする。
- L16-5（実装 / 観点 a。推測を含む）: `web.service_auth.IdTokenAuth` は、メタデータサーバから `identity?audience=` だけでトークンを取る。Compute Engine のメタデータサーバでは、既定（`format=standard`）の ID トークンに `email`・`email_verified` が入らず、`format=full` で入る。Cloud Run のメタデータサーバが既定で `email` を入れるかは未確認（推測）。入らなければ、金庫の検証（契約 §9: `email` が web の SA で、`email_verified` が真）で全呼び出しが 403 になり、必須の点 4 が落ちる。研究報告 4-3 は probe で claim を出して確かめる予定なので、契約 §7 の TEE モードに `format=full` を明記しておけば、スパイクの当日に手戻りしない。
- L16-6（設計 / 観点 a）: AC-23 のスクリプトは、`--project`・`--service-account` を渡さないとプロジェクトと SA を照合しない（契約 §10 の policy が None なら照合しない）。そのため第三者には、許可リストの digest が別のプロジェクト・別の SA で動いていても「合格」と出る（web の方針より弱い）。P-10 でプロジェクト ID は公開なので、許可リストの各リリースにプロジェクトと SA を持たせ、既定で照合する。あわせて、`--direct` でも、控えた証明書だけを信用する接続で `/v1/attestation` を呼ぶ（web と同じ）と、契約 §12 に書く。検証しない接続で呼ぶと、途中で証明書を差し替えられる。

### 問題なしとした観点（根拠と、最も危うい前提）

1. 中継者に対する「検証してからピン留め」: 金庫が launcher に求める nonce は、「呼び手の nonce 1 つ（正規表現で 1 値に限る）＋自分の証明書のハッシュ」に固定されている。web は、自分の新しい nonce と、自分で控えた証明書のハッシュの両方を求める。中継者が得られるのは [n, H_V] か [X, H_V] で、[n, H_R] は作れない。2 回目の接続は控えた 1 枚だけを信用するので、本物の鍵を持たない中継者は TLS を終端できない。launcher を自由に呼べる root のいる debug の VM は `dbgstat`・`STABLE` で、別のイメージは digest の許可リストで落ちる。最も危うい前提は「許可リストのどの版も、本番イメージで、この束ね方の `/v1/attestation` しか持たない」こと。許可リストに寿命がない（C-57）と、将来の版でこの前提が崩れても気づけない。
2. `/v1/attestation` を認証なしにしたことの帰結: 返るトークンの aud は `attestation_audience` で、WIF のプロバイダが許す aud（`https://sts.googleapis.com`）ではない。そのため、STS で KMS の権限に替えられない。nonce は要求した者が決めるので、ほかの者がそのトークンを使い回すこともできない。読み取れるのはプロジェクト・ゾーン・インスタンス名・digest で、どれも P-10 で公開するもの。「ID トークンを未検証の相手に渡さない」という、認証なしにした理由も成り立つ。残るのは発行枠の可用性だけで、X-67（契約 §15 の single-flight）が扱っている。
3. 別のプロジェクトで動く同じイメージ: 本番の WIF の条件と web の方針の両方が `project_id` と金庫の SA を求める。そのため本番の条件の下では、鍵の解放も web の検証も通らない（テスト用の条件の間は C-56）。
4. v15 の 2（C-55）と状態機械: 相手の「本人確認が必要」の提案への `ask_principal` は、§3.5 で有効な手になる（評価を 1 使い、手数には数えない）。確かめの実行条件が守る不等式（評価 ≥ 手数＋途中確認）は、両辺が 1 ずつ減るので保たれる（E−1 ≥ M＋(Q−1)）。「受ける」の後の `accept` の確かめ直しは評価に数えないので、残りの手が 1 でも合意できる。§4.1 の図、指示文の「進め方」の 2、DV-15 の数え方（有効な途中確認を含む手の数 × 2）とも矛盾しない。
5. 次の組み合わせは一致している（L16-4 の 3 点を除く）。§3.3 の `/v1/attestation`（nonce の形・400・429・503・Cloud Run 版にはない）と契約 §3。§10 の TEE 時の web の環境変数と契約 §7。AC-23 で確かめる項目と、契約 §10 の理由の表。
