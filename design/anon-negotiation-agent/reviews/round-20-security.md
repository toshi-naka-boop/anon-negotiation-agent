> critic: claude/design-critic（claude-opus-5-5）— 反証 20 巡目（安全性。codex の代替: codex の設定のモデル gpt-6.1-sol が ChatGPT アカウントで使えず失敗）

## Round 20 — 2026-10-05 — 反証(安全性)

- 範囲: `git diff 1a1b6d9..HEAD -- src static scripts tests config Dockerfile Dockerfile.vault`（コードに触れた 12 コミット）。前提は design.md v22・ledger.md・research/deploy-runbook.md の実施記録（web は公開、agents は web の SA だけ、金庫は外部 IP なしの Confidential Space、Direct VPC egress）。
- 見たもの: 公開中の web の入口（レート制限と全体の枠・SSE の同時本数・面談の開始・攻撃モードと二分探索・メーター・CSP と `/docs`・static/tee.js）、TEE の経路、`scripts/deploy_check.sh` の照合。
- この範囲では `src/vault`・`src/negotiation_core`・`src/web/attested_transport.py`・`Dockerfile.vault` は変わっていない（本番の金庫のイメージ ca5791a から HEAD まで、金庫側の差分は `config/params.toml` の `[agents]` だけ）。TEE の経路は、公開の入口から触れる部分（`/api/tee/attestation` の nonce の転送とピンの扱い）を読み直した。
- 本番への要求・gcloud・試験の実行はしていない（high の疑いが無かったため。以下はすべて静的な読解。数値の見積もりは「推測」と書いた）。

### 指摘

#### 1. [設計 / medium] 金庫を読む匿名の口に全体の上限が無く、1 つの送信元から「web → 金庫の接続」と Firestore の読み出し課金を増幅できる

台帳 I-34 の「GET の再取得への IP ごとの枠」（判断待ち）を、新しい根拠（増幅の倍率・金庫への接続を全員で共有していること・費用が LLM の日次枠の外にあること・公開済みであること）で重大度を上げて再提起する。

- 指摘: セッションなしで金庫を読む口のうち、`GET /v1/demo/negotiations/{nid}/activity`・`GET /v1/demo/negotiations/{nid}/panels`（1 回で両側＝金庫 2 回）・`GET /v1/demo/attack/negotiations/{nid}/events`・`GET /v1/demo/attack/walls/3/{nid}` には枠が全く無い。`POST /v1/demo/meter`（1 回で金庫 20 回を並行）は K4（L19-7）で全体の枠から外れ、送信元ごとの 60 回／10 分だけになった。金庫側は `after_seq=0` なら交渉の文書と、その側のイベントを全件読む（件数の上限なし）。どれも LLM の 1 日の枠（費用の唯一の止め）の外で、Firestore と金庫の負荷には予算アラート（通知だけ）しかない。
- 破綻シナリオ:
  - 誰が: ログインしない匿名の 1 人。VPS 1 台・IPv4 1 つで足りる（IP を変える必要がない）。
  - どの入口から: まず `POST /v1/demo/negotiations`（ケース 2）か `POST /v1/demo/attack/bisection` を 1 回だけ呼んで、架空人物の交渉 ID を 1 つ得る（96 時間使える）。あとは `GET /v1/demo/negotiations/{nid}/panels?candidate_after_seq=0&employer_after_seq=0` を、keep-alive の 150〜200 本の並行で繰り返す。
  - 何が起きるか: (a) 1 要求ごとに web → 金庫が 2 回、Firestore の読み出しが約 45 件（web 側の段の文書 3 件＋金庫側の交渉の文書 2 件と両側のイベント。ケース 2 のリプレイで 19 件と 23 件）。web から金庫への接続は、ピン留めした 1 つの `httpx.AsyncHTTPTransport`（既定の上限 100 接続）を、面談の送信・本物の交渉の作成・本人の活動ログ・レフェリーの手の登録と共有しているので、並行の要求で接続が埋まり、業務の要求は接続待ち（`[web.vault_client] timeout_seconds` の 30 秒）の後に `VaultUnavailableError`（503）になる。金庫（TEE の VM 1 台）も同じ要求で飽和する。(b) 1 秒に 100〜150 要求（web の 1 vCPU と金庫の処理で頭打ちになる見積もり。推測）なら、Firestore の読み出しは 1 秒に 4,500〜7,000 件、1 日に 4〜6 億件。単価を 10 万件あたり 0.03〜0.06 ドルとすると 1 日 120〜350 ドル程度（推測。単価は料金表で要確認）。予算アラート 20,000 円は 1 日で越えるが、止める仕組みはない。
  - SSE の全体 20 本を 10 個の送信元で埋めると、正規の画面も 2 秒ごとの GET の再取得に落ち、その GET も同じく枠が無いので負荷に加わる。IP を変えられる相手なら、メーター（1 要求で金庫 20 回）でも同じことができる。`*.run.app` が IPv6 で受けるなら、/64 の中でアドレスを変えるだけで送信元ごとの枠も意味を失う（推測。未確認）。
- 該当箇所: src/web/activity_api.py:187-210（枠なし・panels は両側）、src/web/attack/router.py:242-247・301-304（枠なし）、src/web/meter_api.py:198-208（20 件を並行）、src/web/limits.py:89（`CLIENT_ONLY_ENTRANCES` に `meter`）、src/vault/store.py:1139-1165（イベントの全件読み）、src/web/attested_transport.py:309（既定の接続上限のまま）、src/web/app.py:255-268（30 秒）、design.md:891（§8.2 の L19-7 の行）、ledger.md:21（I-34 の「残したもの」）
- 直し方の方向:
  - 判定の済んだデモ・攻撃の交渉のイベントは、もう変わらない。web のメモリに (nid, side) ごとに持てば（件数の上限つき・TTL は 96 時間以下）、同じ交渉の読み直しは金庫に行かない。これだけで panels・activity・events・walls/3・meter の増幅がほぼ消える（進行中の交渉は従来どおり読む）。
  - 金庫を読む匿名の口に、送信元ごとの窓（I-34）と、LLM の全体の枠とは別の「読み出しの全体の枠」を足す（L19-7 の意図＝読み出しが LLM と作成の枠を食わない、は保てる）。
  - デモの読み出しには同時実行の上限（セマフォ）を掛け、業務の要求が使う接続を残す。

#### 2. [設計 / medium] 面談の同時 500 件の枠は、匿名の 1 送信元が数時間で埋め、GET で寿命を延ばし続けられる（C-66 の直しが効かない）

台帳 I-34 の「IP ごとの同時に持てる面談の数の上限」（「デプロイの前に」判断のはずが、未決のまま公開）に、新しい根拠（GET が寿命を延ばすので C-66 のアイドルの掃除が効かない・送信元 1 つで足りる）を足して再提起する。

- 指摘: K4 で `/start`（20 回／10 分）と `begin`（10 回／10 分）に送信元ごとの回数の枠が付いたが、どちらも回数の窓で、同時に持てる数の上限ではない。さらに `GET .../interview/state` は枠なしで状態の `touched_at` を更新する（`InterviewStateStore.get`）。そのため、C-66 のもう一方の直し（1 時間のアイドルで読めなくし、見回りで消す）は、持ち主が 1 時間に 1 回読むだけで効かない。`interview_begin` は L19-7 で全体の枠からも外れている。
- 破綻シナリオ:
  - 誰が: 匿名の 1 人（IPv4 1 つ）。
  - 何をすると: 1 分ごとに `GET /start`（クッキーなし → 新しい依頼者 ID）→ `POST /v1/principals/{pid}/interview/begin`（`X-Requested-With` つき）を 1 回ずつ。クッキーと pid を溜め、50 分ごとに全部の pid について `GET /v1/principals/{pid}/interview/state` を 1 回。
  - 何が起きるか: 10 回／10 分 × 50 窓、約 8 時間 20 分で 500 件に達し、以後は面談を始めようとする全員（審査員を含む）が `503 too_many_interviews`。状態は 50 分ごとの GET で消えず、web の再起動（新しいリビジョン）まで続く。再起動しても同じ手順で埋め直せる。送信元が 10 個なら約 50 分、IPv6 の /64 が使えるなら数秒（推測。未確認）。保持の費用は 50 分ごとに 500 回の GET（利用記録の読み出し 1 件ずつ）だけ。
- 該当箇所: src/web/interview/state.py:92-110（`get` が `touched_at` を延ばす・`create` は満杯で `InterviewStoreFull`）、src/web/interview/service.py:234-242（`503 too_many_interviews`）、src/web/interview/api.py:124-130（`/begin` には枠、`GET /state` には無い）、src/web/limits.py:89、config/params.toml:278-279・312、ledger.md:21（I-34）
- 直し方の方向:
  - 送信元ごとに同時に持てる面談の数を数える（`SseConnectionLimiter` と同じ形のメモリの表）。会場の同じ Wi-Fi と兼ね合うので、値は運用メモで上げられる設定にする（I-34 の判断）。
  - アイドルの寿命は「状態を変える POST」だけで延ばし、GET では延ばさない。あるいは作成からの絶対の寿命（例 2 時間）を足す。
  - 満杯のときは、進み具合の無い（プロフィール未入力の）古い状態から追い出す、も候補（進んだ本物の面談は守る）。

#### 3. [実装 / medium] 公開中の入口の「金庫の確認(TEE)」が、オーナーの復号を無条件に「監査ログに主体と時刻が残る」と書いている（X-78・C-62 の限定が落ちている）

- 指摘: static/tee.js の「鍵の排他性」の文は「オーナーは技術的には復号できますが、Cloud KMS の監査ログに主体と時刻が残ります。」と言い切る。設計書 §9（v22）と README は X-78 に従って「監査の設定が有効で除外がない間は」「設定の変更は Admin Activity の監査ログに残る」までに限っており、C-62 の「プロバイダを足した道の記録は金庫と同じ形の subject で、読み解けるのは運営者だけ」もある。画面はこの限定をすべて落としている（台帳 I-37 の文案も同じく限定が無い）。
- 破綻シナリオ:
  - 誰が: 運営者（プロジェクトのオーナー）。
  - 何をすると: (a) KMS の Data Access ログを無効にする（または自分を `exemptedMembers` に入れる）→ `_tee/dek` の包んだ DEK を Decrypt → 設定を戻す。(b) または `vault-tee-pool` にプロバイダを足し、STS で principalSet になって Decrypt。
  - 何が起きるか: (a) では復号そのものの記録は残らない（残るのは監査設定の変更の Admin Activity だけ）。(b) では記録の主体が金庫と同じ形の subject になり、オーナーの名前は出ない。どの記録もこのプロジェクトの中にあり、画面を読む第三者（審査員・利用者）には見えない。画面の文を信じた利用者は「運営者が見れば必ず名前つきで記録される」と受け取り、実データを入れる判断（P-4）の根拠にしてしまう。公開の入口に出ているので、README を読まない人にはこの文だけが届く。
- 該当箇所: static/tee.js:39-43、design.md:963（§9「TEE で言えること」の 2）、README.md:46-47（限定つきの正しい文）、ledger.md:27（I-37 の文案）、archive/ledger-resolved.md の X-78・C-62
- 直し方の方向: README と同じ限定を画面に入れる（例「監査の設定が有効で除外がない間は、復号が主体と時刻つきで残ります。オーナーは監査の設定・鍵の IAM・WIF のプロバイダを変えられ、その変更は Admin Activity に残ります。どの記録もこのプロジェクトの中にあり、第三者は見られません」）。I-37 で文面を確定するときも、X-78・C-62 の限定を残す。tests/test_ui_static.py に、この限定の文言があることの検査を足すと戻らない。

### low のメモ（件数に数えない）

- L-a（deploy_check / tee-b-pool）: 基本ロールのプール権限は Policy Analyzer に出ない（I-36 の実測）のに、直の束縛から拾う役割は `roles/owner`・`roles/iam.workloadIdentityPoolAdmin` だけ。`roles/editor` に同じ権限が入っているなら（未確認）、組織の無いプロジェクトで既定で Editor を持ちがちな主体（Compute Engine の既定の SA・Google APIs のサービス エージェント）があっても「承認済みのオーナーだけ」で OK になる。`POOL_ADMIN_ROLES` に `roles/editor` を足して閉じる側に倒すとよい。scripts/deploy_check.sh:824-849
- L-b（deploy_check / (b)）: 鍵の IAM を自分で書き換えられる主体（`cloudkms.cryptoKeys.setIamPolicy`・`cloudkms.keyRings.setIamPolicy`・`resourcemanager.projects.setIamPolicy` を持つ `roles/cloudkms.admin`・`roles/resourcemanager.projectIamAdmin`・`roles/iam.securityAdmin` など）を列挙しない。C-62 と同じ理屈（いつでも自分に復号権を付けて、使ったら外せる）。いまはオーナー 1 人なので実害はないが、共同の管理者を足す前に (b)-pool と同じ形で列挙するとよい。scripts/deploy_check.sh:808-819
- L-c（手順書と deploy_check）: 実施記録（research/deploy-runbook.md:182）では web に `VAULT_EXPECTED_ZONE`・`VAULT_EXPECTED_INSTANCE` があるが、手順書の `gcloud run deploy web`（同:98）の `--set-env-vars` には無く、`web-vault-env` も見ない。手順書どおりに再デプロイすると、ゾーンとインスタンスの照合が黙って外れ、deploy_check は OK のまま。scripts/deploy_check.sh:465-497
- L-d（deploy_check / healthz-vault）: TEE 版は web の直近 5 分の結果の `verified` だけを見て、動いているダイジェスト（`claims.image_digest`）を (e) の VM のメタデータ・手元の許可表と突き合わせない。メタデータだけ差し替えて VM を再起動していない場合、手元の表で失効させたが web を再デプロイしていない場合に、(e) と healthz-vault が両方 OK になり得る。scripts/deploy_check.sh:631-642・890-915
- L-e（送信元の見分け方）: `web.client_ip` は IPv6 をアドレス単位で鍵にする。`*.run.app` が IPv6 で受けるなら（未確認）、送信元ごとの枠（SSE の 2 本・`session_start`・`interview_begin`・`meter`・攻撃と面談の窓）は /64 の中でアドレスを変えるだけで外れ、全体の枠の無い入口（指摘 1・2）では上限が無くなる。IPv6 は /64 にまとめて鍵にするのが安い。src/web/client_ip.py
- L-f（tee.js）: 「言えないこと」は 2 つだけで、§9 の「古い版への巻き戻しと削除は防げない」「運営側のコードは手を登録できる」「テンプレートは書き換えられる」が画面にない。README の該当の節へのリンクを置くだけでもよい。static/tee.js:49-56
- L-g（deploy_check / agents-url）: 照合は手元の作業ツリーの `config/params.toml` を読むので、デプロイしたイメージのコミットと手元がずれていても気づかない。scripts/deploy_check.sh:500-517

### 確かめて問題なしとした観点（最も危うい前提つき）

- SSE の席（C-65）: 権限の確認より先に取り、確認の失敗（`except BaseException`）と応答の終わり（`_SlotEventSourceResponse.__call__` の finally）の両方で戻し、`SseSlot.release` は 1 回しか効かない。席が漏れて 20 本が埋まったままになる道は見つからなかった。最も危うい前提は「返した応答オブジェクトを Starlette が必ず呼ぶ」こと（いまのミドルウェアは純粋な ASGI なので成り立つ。BaseHTTPMiddleware を足すと崩れ得る）。
- 旧い送信口の撤去（X-81）: `InterviewSubmitRequest` を HTTP の本文として受ける経路は残っていない（`submit_policy` は内部と試験の補助だけ）。最も危うい前提は、今後の経路の追加で本文として受けないこと（AC-01 の経路の走査が守る）。
- デモ・攻撃の読み出し（二分探索・メーター・段・SSE）は、web の段の文書と金庫のデモ用の口（mode と `is_fictional`）の 2 段で確かめ、本物の依頼者の交渉を読まない。二分探索の冪等キーは `attack-scripted:` の名前空間で、`quote(..., safe='')` で 1 つのパスの部分にして送るので、本物のキーとの衝突やパスの書き換えはない。台本のレフェリーは LLM を呼ばず（役割の外は例外）、`count_llm_calls=False` でも費用の穴にならない。
- 決着処理の移動（X-84）: 本物の候補者は、依頼者のロックの下で、利用記録が使える状態のときだけ書く。フックはレフェリーが操作ごとのロックを離した後に呼ばれ、削除の流れはレフェリーのタスクを待たないので、行き詰まりは起きない。最も危うい前提は「削除の流れがロックを持ったままレフェリーのタスクを待たない」こと（web/locks.py の規則）。
- TEE の公開の口: 利用者が選ぶ nonce でピンを外させる道は見つからなかった（nonce を金庫の証明書のハッシュと同じにしても、`eat_nonce` に両方が入るので照合は通る。launcher が重複を拒めば金庫は 503 → 一時的な失敗としてピンは保たれる）。転送は送信元ごと 10 秒・全体 2 秒。tee.js は DOM の API だけで描き（innerHTML なし）、リンクは https だけ、コマンドに差し込む値は形を確かめている（置き換え用の `<…>` はシェルでは構文エラーで止まり、何も実行されない）。JWT は画面に出ない。
- `rate_limits` の文書 ID の HMAC（鍵は署名の鍵からラベルで派生）、本番の組み立ての `docs=False`（`/docs`・`/redoc`・`/openapi.json` なし）、deploy_check の `containerConcurrency=200` の照合、(b)-pool の空の結果を NG にする直し、(h) の停止→開始は、意図どおり。
