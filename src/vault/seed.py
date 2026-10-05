"""金庫の起動時の、架空人物のテンプレートの投入(design.md §3.7・§8.4。台帳 P-15)。

架空人物(デモの候補者・攻撃モードの相手・フィクスチャの求人)のテンプレートは、イメージに焼いた `fixtures/case*.toml` から、
金庫が起動のたびに vault-db の `templates/{template_id}` へ書く。運営者の手元から書く経路は持たない(TEE の外から vault-db に
書く経路を増やさない)。テンプレートは公開フィクスチャなので、封印しない(§3.8)。イメージのダイジェストが、フィクスチャの
中身まで覆う。

- 読み込みと検証は vault.fixtures と同じ(形・グリッド外の値・同じ組の重複・ポリシーの矛盾)。すべてのファイルを検証し終えてから
  書くので、壊れたファイルが 1 つでもあれば、1 件も書かない。
- 冪等: 文書の中身が、これから書く中身と完全に同じなら書かない(読んで比べる)。違えば(知らない項目が残っている場合も)置き換える。
  テンプレートは読み取り専用で、交渉は作成のときにテンプレートから写したコピーで動くので、上書きしてよい(§3.7)。
- 同じ template_id が複数のファイルにあるときは、中身が同じなら 1 件として扱い、違えば拒否する(後のファイルが黙って勝たないように)。
- ログに出すのは、書いた件数とスキップした件数だけ(値は出さない)。失敗(ファイルの検証エラー・Firestore の失敗)は例外にする。
  ファイルの検証に失敗したときは、ファイル名と例外の型名をログに書く。
- 削除はしない: フィクスチャから消えたテンプレートは vault-db に残る。
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from google.cloud import firestore

from vault.fixtures import FIXTURES_DIRECTORY, load_case_fixture
from vault.models import Template
from vault.serialization import model_to_firestore
from vault.templates import TEMPLATES_COLLECTION, put_template

logger = logging.getLogger(__name__)

_CASE_FILE_NAME = re.compile(r"case([1-9][0-9]*)\.toml")


@dataclass(frozen=True)
class SeedResult:
    """投入の件数。written: 書いた(新規、または置き換え)。skipped: 中身が同じなので書かなかった。"""

    written: int
    skipped: int


def _load_templates(directory: Path) -> list[Template]:
    """directory の `case*.toml` をすべて読んで検証し、テンプレートの一覧にする(ファイル名の順)。1 つも無ければ FileNotFoundError。"""
    paths = sorted(directory.glob("case*.toml"))
    if not paths:
        raise FileNotFoundError(f"no fixture file (case*.toml) in {directory}")
    templates: dict[str, Template] = {}
    for path in paths:
        try:
            matched = _CASE_FILE_NAME.fullmatch(path.name)
            if matched is None:
                raise ValueError(f"a fixture file must be named case<N>.toml: {path.name}")
            for template in load_case_fixture(int(matched.group(1)), directory).templates():
                if templates.setdefault(template.template_id, template) != template:
                    raise ValueError(f"the template_id {template.template_id!r} has different contents in two places")
        except Exception as exc:
            logger.error("template seeding failed: the fixture file %s is not usable (%s)", path.name, type(exc).__name__)
            raise
    return list(templates.values())


def seed_templates(db: firestore.Client, fixtures_dir: Path = FIXTURES_DIRECTORY) -> SeedResult:
    """fixtures_dir の `case*.toml` のテンプレートを、vault-db の `templates/{template_id}` に冪等に書く(上の説明のとおり)。"""
    templates = _load_templates(fixtures_dir)
    written = skipped = 0
    for template in templates:
        stored = db.collection(TEMPLATES_COLLECTION).document(template.template_id).get().to_dict()  # なければ None
        if stored == model_to_firestore(template):
            skipped += 1
        else:
            put_template(db, template)
            written += 1
    logger.info("templates seeded: written=%d skipped=%d", written, skipped)
    return SeedResult(written=written, skipped=skipped)
