"""交渉エージェントの指示文の置き場所と読み込み(design.md §4.2)。

指示文は、このディレクトリの `{role}.md`(role は candidate・employer・attacker)に置く。
1c(受信口と検証)では、置き場所と読み込みだけを作った。中身は仮の最小の文で、実装計画 ② で
書き直す(指示文の要点は §4.2)。

指示文は固定で、入力(TurnInput)によって変わらない。LLM の文脈は「指示文＋TurnInput」だけになる
(壁 2 の画面表示は、この 2 つをそのまま見せれば正確になる。§4.2)。
"""

from pathlib import Path

from agents.wire import ROLES, Role

_DIRECTORY = Path(__file__).resolve().parent


def load_instruction(role: Role) -> str:
    """role の指示文(前後の空白を除いた全文)を読み込む。ファイルがなければ FileNotFoundError。"""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    return (_DIRECTORY / f"{role}.md").read_text(encoding="utf-8").strip()
