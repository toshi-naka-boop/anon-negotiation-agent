# CLAUDE.md — Negocie（anon-negotiation-agent）

このファイルは、プロジェクト固有の決まりだけを書く。全体の方針は `~/.claude/CLAUDE.md`。

## 画面（static/）を触るときの決まり

- UX の設計は **`DESIGN.md`** が正。画面を作る・直すときは、該当の章（§4 画面と 4 状態、§5 開示、§6 透明性、§7 言える・言えない、§8 文言、§9 トークン、§10 やらないこと）を先に読み、終わったら §11 のチェックリストで確かめる。
- 仕組み（丸め・金庫・交渉・段階開示・削除）の根拠は `design/anon-negotiation-agent/design.md`。画面は仕組みを変えない。
- 画面の試験は `tests/test_ui_static.py`、静的検査は `scripts/check_no_web_storage.sh`。設計書が約束する文言（`SCREEN_TEXT`）・ナビの 3 つの組・入口のカード・区画の id は、試験が固定している。変えるなら設計書と試験を同時に直す。
- ページは 5 つ固定（`/` `/interview` `/me` `/demo` `/attack`。`src/web/app.py` の `PAGE_FILES`）。画面を増やすときは、ページの中の画面切り替え（`/me` の hash）で作る。
- 書かないもの: インラインの script・style、innerHTML、ブラウザの保存領域への書き込み、「絶対」「完全」「保証」、技術用語の主表示（TEE・attestation・グリッド・アンカー・ポリシー・A2A）、スコア・診断・カウントダウン。
- 製品名は「Negocie（ネゴシエ）」。タブ名と通知の差出人名は「Negocie」だけ。

## 言語

- 応答・コミットメッセージ・コメントは日本語。コード・識別子は英語。
