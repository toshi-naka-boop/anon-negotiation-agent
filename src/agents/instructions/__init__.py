"""交渉エージェントの指示文の置き場所と読み込み(design.md §4.2)。

指示文は、このディレクトリの `{role}.md`(role は candidate・employer・attacker)に置く。
candidate・employer の中身は、実装計画 ② で §4.2 の要点と台帳の決定(C-30 の評価の使い方、C-34 の
年収と他の軸を一緒に動かす探し方、C-40 の last_invalid)に沿って書き、③-0 で v14 に合わせて書き直した
(計画 plan・決定 decide の 2 種類の呼び出しと checked、`check` という手はないこと、output_truncated、
残りの評価のうち残りの手数と途中確認の分を残す配分。台帳 X-45・C-47・C-53)。attacker は、攻撃モード(③。§8.2)で書いた本番の指示文で、
求人担当(審査員)が自然文で書く指示(`principal_instruction`)を、毎手番の入力で受け取る(計画・決定の 2 種類の呼び出しは candidate・employer と同じ)。

指示文は側ごとに 1 つで、計画と決定で共有する。固定で、入力(TurnInput)によって変わらない。LLM の文脈は
「指示文＋TurnInput」だけになる(壁 2 の画面表示は、この 2 つをそのまま見せれば正確になる。§4.2)。
"""

from pathlib import Path

from agents.wire import ROLES, Role

_DIRECTORY = Path(__file__).resolve().parent


def load_instruction(role: Role) -> str:
    """role の指示文(前後の空白を除いた全文)を読み込む。ファイルがなければ FileNotFoundError。"""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    return (_DIRECTORY / f"{role}.md").read_text(encoding="utf-8").strip()
