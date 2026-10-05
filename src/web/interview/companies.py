"""ブロック先に選べる企業の一覧(design.md §5 の 8・§6.1。FR-07)。

求人の一覧ではなく、企業の一覧から選ぶ。fixtures/ の求人(case*.toml)から、企業の ID と名前を集める。公開求人・非公開求人の有無に
関係なく全企業を載せ、求人があるかどうか(件数・非公開かどうか)は示さない(現職企業が非公開求人しか出していなくても選べるように)。
登録できるのは本人の現職企業だけと、利用規約と画面の説明で定める(技術的には縛らない。P-1 の回答)。
"""

import tomllib
from pathlib import Path

from web.interview.templates import FIXTURES_DIRECTORY


def list_companies(directory: Path = FIXTURES_DIRECTORY) -> list[dict[str, str]]:
    """全企業の [{company_id, company_name}]。企業の名前の順。同じ企業 ID は 1 件にまとめる。求人の情報は含めない。"""
    names: dict[str, str] = {}
    for path in sorted(directory.glob("case*.toml")):
        employer = tomllib.loads(path.read_text(encoding="utf-8"))["employer"]
        names.setdefault(employer["company_id"], employer["company_name"])
    return [
        {"company_id": company_id, "company_name": name}
        for company_id, name in sorted(names.items(), key=lambda item: (item[1], item[0]))
    ]
