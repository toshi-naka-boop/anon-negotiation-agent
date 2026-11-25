# Negocie(ネゴシエ)— 本音を預けられる転職エージェント

(リポジトリ名は anon-negotiation-agent(匿名交渉エージェント)。画面と提出文では「Negocie」と呼びます。)

> 話した本音が相手にどこまで伝わるか、人間のエージェントは約束できない。このエージェントは、あなたが決めた粗さまでしか伝えない。疑うなら、攻撃してみてください。

**誰の、何の仕事が、どう楽になるか**: 転職の候補者は、「最低いくらなら受けるか」といった本音を、相手に見せずに預けられます。
AI のエージェントが、求人側のエージェントと、本音の条件を明かさないまま、合意できそうな組み合わせを探します。
本人は、条件を相手に口にして足元を見られる心配をせずに、「会う価値があるか」の見込みだけを受け取ります。

転職の候補者と求人側は、本音の条件(最低年収、許せる当直の回数など)を口に出さないまま、
「言えば通ったかもしれない話」を逃しがちです。このシステムは、双方が本音を明かさずに、
「合意できそうか」だけを確かめます。

- 候補者は、面談(LLM)で本音を話します。生の値は面談の間だけ扱い、送信のあとは保存せず、ブラウザにも残しません。
- 本音は、共通のマス目(グリッド。年収なら 50 万円の幅など、あらかじめ決めた粗さの区切り)に丸めてから、**金庫**に預けます。丸め済みのポリシーを持つのは金庫だけです。
  金庫は LLM を使わない決定的なコードで、提案もマス目の上に限るので、丸めても判定は 1 件も変わりません。
  相手に分かるのは、最大でもマス目 1 つぶんです。
- 候補者側と求人側の交渉エージェント(LLM)は、金庫に「この組み合わせは受けられるか」を聞き、返ってくる 3 値だけを頼りに、
  合意できる組み合わせを探します。エージェントは、依頼者の生の値も、相手のポリシーも見ません(見えるのは、提案と 3 値の答えだけです)。
- 結果は「見込み(高・中・なし)と組み合わせ 1 つ」です。匿名の職務要約、氏名・連絡先は、段階を踏んで、
  双方が操作したときだけ開きます(ただし、この版の段 2 は模擬の表示です。下の「この版でできないこと」を参照)。

## 試す(ログイン不要)

- 公開デモ: https://web-341888860511.asia-northeast1.run.app/ (ハッカソン提出時の公開先です。いま動いているかは、運営者の運用に依ります)
- 試し方は 3 つです。画面の入口(`/`)から行けます。
  1. 「面談を始める」: 実際に条件を預けられます(1 画面に 1 問。年収の 3 問から)。預けたあとは「自分の交渉」で、求人を選んで交渉を始められます。
  2. 「求人担当の画面(デモ)」: 架空の求人と架空の候補者の交渉を、ケース 1〜3 から選んで見られます。
  3. 「攻撃してみる」: 求人担当になったつもりで、架空の候補者の年収を探る攻撃を試せます。あなたの本音は使いません。
- 自分の手元で動かすには、下の「ローカルでの動かし方」を見てください(エンジニア向けです)。

## この版でできないこと(制約)

- **求人は架空の 2 件だけ**です(`fixtures/` の `case*.toml`)。求人側の登録の画面・面談はありません。求人側の応答は、台本どおりに自動で返ります。
  本物の求人側が本音を預ける画面は、これから作るものです(作者の判断が要ります)。
- **段 2(氏名と連絡先)は模擬の表示**です。この版は、あなたの氏名・連絡先を集めていないので、相手に渡るものがありません。合意しても、実際に会う所までは進めません。
- 自分の交渉の「求人分析」は、サンプルの数字です(本物の求人データの集計ではありません)。
- 途中確認(「この組み合わせなら受けますか」)には、質問が出てから 24 時間以内に答える必要があります。通知(メールなど)は届かないので、交渉中は 1 日に 1 回は画面を開いてください。過ぎると、その交渉は時間切れで終わります(預けた条件は残ります)。
- 本人は、ブラウザのクッキーで見分けます。クッキーを消す・別の端末やブラウザで開くと、預けた条件と交渉には戻れません(クッキーの有効期間は面談を始めてから 29 日。預けたデータは、最後に使ってから 30 日で自動で消えます。「自分の交渉」の「データを消す」で、すぐ消すこともできます)。
- 面談の AI 処理は Google Cloud(Vertex AI)で行われ、不正利用の監視のために、一定期間記録され得ます。このシステムが保存しないのは生の値までで、Google 側の記録は消せません。
- TEE(後述)で動いているかどうかは、公開デモの入口の「詳しく見る: 金庫の確認(TEE)」の表示が正です。

## 守りの仕組み

**金庫の 3 値**: 金庫は、組み合わせごとに「受けられる」「受けられない」「本人確認が必要」のどれかだけを返します。
受けるアンカー(受けると決めた境目)を 1 つでも満たせば「受けられる」、受けないアンカーに含まれれば「受けられない」、
どちらでもなければ「本人確認が必要」で、このときだけ本人に途中確認します。

**3 つの壁**(攻撃画面で、審査員が自分で試せます)

1. 壁 1: A2A メッセージの JSON を自分で編集して、候補者側エージェントの受信口に送れます。余計な項目や自由文は、LLM が動く前に拒否されます。
2. 壁 2: 候補者側エージェントの直近の手番で、LLM に渡った文脈の全文が見えます。生の数値も、ID も、自由文も入っていません。
3. 壁 3: 金庫は、丸め済みでしか答えません。金庫の答えをすべて見られたとしても、攻撃者が絞り込めるのはマス目 1 つまでです(推定区間メーター)。

## 言えること・言えないこと

**運営者についての限界**(隠さずに書きます)

- 面談の間、運営側の `web` は生の値に触れます。Vertex AI 側では、不正利用監視のためにプロンプトが一定期間記録され得ます。
  言えるのは「このシステムは生の値を保存しない」までで、「どこにも残らない」とは言いません。
- `web` のレフェリーは運営側のコードなので、両側の手を金庫に登録できます。運営者は、本人向けの読み出し口とイベント列の読み出し口で、
  丸め済みポリシーと交渉の記録(マス目の上の値)を読めます。保存データを古い版に戻したり、依頼者を作り直したりして、予算を回復することもできます。
- どの場合も、得られる情報は丸め済みポリシーまでです(丸めの性質で上限が決まります)。予算は運営者への歯止めではなく、費用の制御と早期警告です。

**TEE(Confidential Space)で言えること**(金庫を TEE で動かす構成の場合。この構成は、2026-10-03〜04 のスパイクで実測しました〔台帳 I-14・I-36〕。公開先が TEE 版か Cloud Run 版かは、画面の「金庫の確認(TEE)」が示します。Cloud Run 版のときは、TEE の確認は出ません)

1. 動いているもの(第三者が確かめられる): 「この VM では、Confidential Space の本番イメージで、ダイジェスト D の金庫のコンテナが、
   コマンド・環境変数の上書きなしに動いている」。下の `verify_attestation.py` で確かめます。
2. 鍵の排他性(プロジェクトの**オーナー以外**について。運営者の設定に依存し、運営者が `scripts/deploy_check.sh` で照合します): 保存データの鍵(KEK)を使える主体は、
   D の principalSet とプロジェクトのオーナーだけです。オーナー自身は、基本ロールで復号できます。これは TEE でも防げません。
   オーナーに対して言えるのは、「監査の設定が有効で除外がない間は、復号が Cloud KMS の Data Access 監査ログに主体と時刻つきで残り、
   設定の変更は Admin Activity の監査ログに残る」までです(技術的な排他ではなく、記録による抑止。Data Access ログの保持は既定で 30 日)。
   組織の配下では、IAM の拒否ポリシーでオーナーの復号を拒みます(拒否ポリシーを外せる主体はいますが、その変更も Admin Activity に残ります)。
3. ソースの由来(いまは運営者の申告): 「D は、運営者がコミット C から作ったと記録したもの」です。第三者が確かめられるのは、
   GitHub の証明つきビルドか再現ビルドを入れてからです。それまで、「許可リストにあるダイジェストが動いている。コミットとの対応は運営者の記録」と書き、
   「公開されたコードで動く」とは書きません。

**TEE でも言えないこと**

- 運営側のコードは、読み出し口を通せば、丸め済みポリシーと交渉の記録を読めます。封印が守るのは、オーナー以外の主体
  (ほかの管理者、漏れた資格情報、Firestore のエクスポートやバックアップを手にした者)が Firestore を直接読む場合です。古い版への巻き戻しと削除は防げません。
- 段階開示の状態と開示台帳は、TEE の外(`web` 側)にあります。面談中の生の値も、Vertex AI 側の記録も、TEE とは関係がありません。
- 確かめられるのは「このダイジェストの TEE が存在し、与えた nonce に応答した」ことまでです。「`web` がその TEE にだけデータを送っている」ことの証明にはなりません。
  失効したイメージが、最長およそ 40 分動き続けることはあり得ます。
- debug イメージで動かした間に作った鍵は、運営者が見られたものとして扱い、本物の依頼者のデータを入れる前に、本番イメージで作り直します。
- Firestore を直接読める者には、どの依頼者 ID が、いつ、どの求人と交渉し、合意に至ったか、手数がいくつか、が見えます(封印するのは、本物の依頼者の機微な項目だけ)。
- 漏洩の上限(マス目 1 つ)は丸めで成り立つので、TEE の有無に関係なく変わりません。

## 第三者が確かめる手順

```
uv run python scripts/verify_attestation.py --web https://<web の URL> --project <プロジェクト ID> --service-account <金庫のサービスアカウントのメール>
```

終了コード 0 なら、attestation の JWT が Google の鍵で検証でき、渡した nonce が入っていて、`swname`・`dbgstat`・`STABLE`・`hwmodel`・プロジェクト・サービスアカウントが
本番の条件で、イメージのダイジェストが `deploy/vault-releases.json` の `active` な要素にあります。`--web` は、金庫の TLS 証明書を観測できないので、証明書との結び付きは確かめません(出力にもそう書きます)。

- `deploy/vault-releases.json`: 金庫のイメージのダイジェストと、元のコミットの対応表(運営者の記録)。失効したものは `revoked` です。
- `deploy/expected-kms-principals.json`: KEK を使えるはずの主体の正本(承認済みのオーナーと、`active` なダイジェストの principalSet の形)。公開リポジトリには雛形(`<…>` のまま)だけを置き、実値(オーナーのメールとプロジェクト番号)は手元の git 管理外のファイルに書いて、`scripts/deploy_check.sh` には環境変数 `EXPECTED_KMS_PRINCIPALS_FILE` でその場所を渡す(台帳 P-20 の判断、2026-10-04)。
  運営者が `scripts/deploy_check.sh` で、Cloud Asset の Policy Analyzer の列挙と完全一致で照合します。第三者は、中身を読めますが、IAM との一致は運営者の確認です。
  - 埋め方: `owners` にオーナーのメールを書き、`<PROJECT_NUMBER>` をプロジェクト番号に置き換えます。`{digest}` はそのまま残します(スクリプトが `active` なダイジェストごとに展開します)。
    現在の IAM から自動では作りません(手で書く正本)。リポジトリは公開なので、載せるメールは、公開してよいものにします。
    `bash scripts/deploy_check.sh --show-expected` で、展開した期待集合を確かめられます。
- `scripts/deploy_check.sh`: 運営者のデプロイの確認(`bash scripts/deploy_check.sh --list` で項目の一覧)。

## 構成

| サービス | 場所 | 役割 |
|---|---|---|
| web | `src/web/`(+ 画面 `static/`) | 画面と API。面談(LLM)・交渉のレフェリー・段階開示・開示台帳。運営側のコード。Firestore の `(default)` を使う |
| agents | `src/agents/` | 交渉エージェント(候補者側・求人側・攻撃用の求人側)。A2A の受信口。Gemini(ADK) |
| vault(金庫) | `src/vault/` | 丸め済みのポリシーと交渉の状態を持ち、3 値で答える決定的なコード(LLM を使わない)。Firestore の `vault-db` を使う。TEE 版は `src/vault/tee/` |
| 共通 | `src/negotiation_core/` | 丸め・ポリシー・メッセージの型など、3 つが共有する部分 |

- `config/params.toml`: 暫定値(期限・枠・保存期間など)。`fixtures/`: 架空の人物・求人・面談の問い。`static/`: 画面(ビルド工程なし)。
- `scripts/`: デモ・リプレイ・検査・デプロイの確認。`tests/`: 試験。`design/`: 設計書と批評の記録。`deploy/`: 金庫のイメージの許可リストなど。
- ページは 5 つ(`/` `/interview` `/me` `/demo` `/attack`)。

## 環境変数

**web**(`uvicorn web.app:create_app_from_env --factory --workers 1`。Dockerfile の既定の起動コマンド。依頼者ごとのロックがプロセスの中にあるので、workers は 1)

| 名前 | 必須 | 内容 |
|---|---|---|
| `SESSION_SIGNING_KEY` | 必須 | セッションのクッキーの署名鍵。32 バイト以上の乱数(base64url)。なければ・弱ければ起動しません。作り方: `python -c "import secrets; print(secrets.token_urlsafe(32))"`。値はリポジトリに入れず、環境変数かシークレット管理で渡し、漏れたら作り直します(作り直すと、既存のクッキーは使えなくなります) |
| `VAULT_BASE_URL` | 必須 | 金庫の URL |
| `SERVICE_AUTH_ENABLED` | 任意(既定 `true`) | Cloud Run の ID トークンで、金庫・agents を呼ぶ。ローカルでは `false` |
| `VAULT_TEE` | 任意(既定 `false`) | 金庫が TEE かどうか。`true` のとき、`VAULT_BASE_URL` は https、`VAULT_SERVICE_ACCOUNT`(金庫のサービスアカウントのメール)と `GOOGLE_CLOUD_PROJECT` も必須 |
| `VAULT_RELEASES_FILE`・`VAULT_EXPECTED_ZONE`・`VAULT_EXPECTED_INSTANCE`・`GITHUB_REPO_URL` | 任意(TEE のとき) | digest の許可リスト(既定 `deploy/vault-releases.json`)、期待するゾーン・インスタンス、コミットのリンクの土台 |

**vault**(`uvicorn vault.app:create_app_from_env --factory`。TEE 版は `python -m vault.tee.main`)

| 名前 | 必須 | 内容 |
|---|---|---|
| `VAULT_SEED_TEMPLATES` | 任意(既定 `true`) | 起動時に、`fixtures/` の架空人物のテンプレートを `vault-db` に書く。ローカルでは `false` にできる |

Firestore の接続先は、Google のクライアントが環境から決めます(Cloud Run ではサービスのプロジェクト)。

**ローカルの試験・デモ**

| 名前 | 内容 |
|---|---|
| `FIRESTORE_EMULATOR_JAVA` | エミュレータを動かす java(JDK 21)のパス。未設定なら `JAVA_HOME`・`PATH` の java を探す |
| `FIRESTORE_EMULATOR_JAR` | `cloud-firestore-emulator.jar` のパス。未設定なら、Google Cloud SDK のよくある場所を探す |
| `GOOGLE_GENAI_USE_VERTEXAI`・`GOOGLE_CLOUD_PROJECT`・`GOOGLE_CLOUD_LOCATION` | `--live`(本物の Gemini)のときだけ。費用がかかる |

## ローカルでの動かし方

要るもの: Python 3.12 と [uv](https://docs.astral.sh/uv/)。試験・デモ・ローカルサーバは、Firestore エミュレータを使うので、JDK 21 と、Google Cloud SDK のエミュレータの jar も要ります。本物の GCP には接続しません。

- JDK 21: macOS は `brew install openjdk@21`、Linux は各ディストリビューションの `openjdk-21`。
- jar: [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) を入れて `gcloud components install cloud-firestore-emulator`。
- java と jar の場所は、`PATH`・`JAVA_HOME` と、SDK のよくある場所(Homebrew・apt・`~/google-cloud-sdk` など)から自動で探します。見つからないとき、または別の場所のものを使うときは、
  環境変数 `FIRESTORE_EMULATOR_JAVA`・`FIRESTORE_EMULATOR_JAR` で指定します(`tests/conftest.py` と `scripts/run_demo.py`〔`serve_local.py`・`canary_scan.py` も同じ〕が `scripts/emulator_locator.py` を共有しています)。
  見つからなかったときは、何が足りないかと入れ方を表示して止まります。

```
export FIRESTORE_EMULATOR_JAR=/path/to/cloud-firestore-emulator.jar   # 自動で見つかるなら不要
uv sync
uv run pytest -q                                      # 全試験(エミュレータは自動で起動・停止)
uv run ruff check src scripts tests                   # 静的検査(構文・未定義名・未使用の import)
uv run python scripts/run_demo.py --case 1            # ケース 1 を、台本のエージェントで通す(Gemini は呼ばない)
uv run python scripts/run_demo.py --case 1 --replay   # 記録(fixtures/replays/)を、記録どおりの間隔で流す
uv run python scripts/run_demo.py --case 1 --live --runs 2 --judge   # 本物の Gemini(費用がかかる。GOOGLE_GENAI_USE_VERTEXAI・GOOGLE_CLOUD_PROJECT・GOOGLE_CLOUD_LOCATION が要る)
uv run python scripts/replay_check.py                 # リプレイを 3 回再生して、イベント列のハッシュが一致することを確かめる
uv run python scripts/canary_scan.py                  # 生の値のカナリアが、保存・ログ・メモリ・スパンのどこにも残らないことを確かめる
bash scripts/check_no_web_storage.sh                  # 画面(static/)がブラウザの保存領域に書かないことの静的な検査(AC-02)
uv run python scripts/serve_local.py                  # 画面(static/)を、手元のブラウザで確かめる開発用サーバ(http://127.0.0.1:8080。台本のエージェント・面談はスタブ。--live で本物の Gemini)
```

試験は 3,902 件(2026-11-25 時点。`uv run pytest --collect-only -q` で数えた値)です。Linux での全体の実行は、約 12 分かかりました(環境によります)。
`uv run pytest -q tests/test_emulator_locator.py tests/test_vault_app_docs.py` のように、エミュレータの要らない試験だけを先に動かすこともできます。

CI は置いていません(品質の確認は、手元の上の手順に頼っています)。

### 運用の前提

- web の `X-Forwarded-For` の扱いは、Cloud Run に直接つなぐ構成を前提にしています(`src/web/client_ip.py`)。前に外部 HTTPS ロードバランサーを置くと、全員が 1 つの枠を共有してしまうので、置くなら設計の見直しが要ります。

## 設計書と台帳

- `design/anon-negotiation-agent/design.md`: 設計書(承認済み)。丸め・金庫・信頼境界・TEE の主張の根拠はここです。
- `design/anon-negotiation-agent/ledger.md`: 反論台帳(未解決の指摘と、実装で分かったこと)。`spec.md` は仕様書、`research/` は調査です。

## ライセンス

未定。
