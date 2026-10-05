"""金庫のイメージの digest と、その元のコミットの対応を、deploy/vault-releases.json に 1 件追記する。使わなくなった digest は失効させる(design.md §9、research/tee-spike.md 6-3 の L0、契約 §13)。

    uv run python scripts/tee_record_release.py --digest sha256:<64 桁の 16 進> --commit <40 桁の 16 進>
    uv run python scripts/tee_record_release.py --digest ... --commit ... [--built-at 2026-10-03T12:00:00Z] [--releases deploy/vault-releases.json]
    uv run python scripts/tee_record_release.py --revoke sha256:<64 桁の 16 進>        # 表にある digest を失効させる

この表は、web の検証(negotiation_core.attestation)と scripts/verify_attestation.py が「許可する digest」として読む。
digest は Cloud Build で作った後に分かるので、ビルドの後にこのスクリプトで追記し、別のコミットにする(ビルドの元のコミットの中には書けない)。
表が言えるのは「運営者が、このコミットから作ったと記録した」ことまで。第三者が同じコミットから作り直しても、同じ digest にはならない(R8)。

表の項目: {"digest", "commit", "built_at", "status": "active" | "revoked"}。失効した項目には "revoked_at" がつく。
- 追記は releases の末尾に、status を "active" にして書く。JSON は indent=2・末尾に改行(既存のファイルと同じ形)。
- 同じ digest がすでにあれば(active でも revoked でも)、上書きも復活もせず、何も書かずに終わる(終了コード 0。メッセージは already recorded)。
- --revoke <digest>: 表にある digest の status を "revoked" にし、revoked_at(現在の UTC)をつける。項目は消さない(履歴として残す)。
  表に無い digest は終了コード 2。すでに revoked なら、何も書かずに終わる(終了コード 0。メッセージは already revoked)。
- 形が違えば、何も書かずに終了コード 2: digest は `sha256:` + 小文字の 16 進 64 桁、commit は小文字の 16 進 40 桁、
  --built-at はタイムゾーンつきの ISO 8601(省略時は現在の UTC)。--revoke は --digest・--commit・--built-at と一緒に使えない。
  表のファイルが無い・読めない・形が違う(digest が文字列でない、status が active・revoked でない)ときも 2。

終了コード: 0 = 追記した・失効させた、またはすでにその状態。2 = 形の違い・表に無い digest(何も書かない)。
GCP には接続しない。
"""

import argparse
import datetime as dt
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELEASES_PATH = PROJECT_ROOT / "deploy" / "vault-releases.json"

DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
STATUSES = ("active", "revoked")


class ReleaseTableError(ValueError):
    """リリースの表のファイルが無い・読めない・形が違う。"""


def has_timezone(timestamp: str) -> bool:
    """ISO 8601 として読めて、タイムゾーンがついているか(2026-10-03T12:00:00Z や +09:00)。"""
    try:
        return dt.datetime.fromisoformat(timestamp).tzinfo is not None
    except ValueError:
        return False


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_table(path: Path) -> dict:
    """表のファイルを読む。`{"releases": [{"digest": ..., "status": ...}, ...]}` の形でなければ ReleaseTableError。"""
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseTableError(f"{path} がありません") from exc
    except (OSError, ValueError) as exc:  # JSON の構文の誤り(JSONDecodeError)は ValueError の一種
        raise ReleaseTableError(f"{path} を読めません({type(exc).__name__})") from exc
    if not isinstance(table, dict) or not isinstance(table.get("releases"), list):
        raise ReleaseTableError(f'{path} は {{"releases": [...]}} の形ではありません')
    if not all(
        isinstance(entry, dict) and isinstance(entry.get("digest"), str) and entry.get("status") in STATUSES
        for entry in table["releases"]
    ):
        raise ReleaseTableError(f"{path} の releases に、digest が文字列でない項目、または status が active・revoked でない項目があります")
    return table


def write_table(path: Path, table: dict) -> None:
    path.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="金庫のイメージの digest と元のコミットの対応を、リリースの表に追記する(--revoke で失効させる)。")
    parser.add_argument("--digest", help="追記するイメージの digest(sha256: + 小文字の 16 進 64 桁)")
    parser.add_argument("--commit", help="ビルドの元のコミット(小文字の 16 進 40 桁。git rev-parse HEAD)")
    parser.add_argument("--built-at", help="ビルドの時刻(タイムゾーンつきの ISO 8601。省略時は現在の UTC)")
    parser.add_argument("--revoke", help="失効させる digest(表にあるもの)。--digest・--commit・--built-at とは一緒に使えない")
    parser.add_argument(
        "--releases", type=Path, default=DEFAULT_RELEASES_PATH, help="リリースの表(既定 deploy/vault-releases.json)"
    )
    args = parser.parse_args(argv)

    if args.revoke is not None:
        if any(value is not None for value in (args.digest, args.commit, args.built_at)):
            parser.error("--revoke は --digest・--commit・--built-at と一緒には使えません")
        if not DIGEST_PATTERN.fullmatch(args.revoke):
            parser.error("--revoke は sha256: に続けて、小文字の 16 進 64 桁にしてください")
        return args

    if args.digest is None or args.commit is None:
        parser.error("追記するには --digest と --commit が要ります(失効させるときは --revoke)")
    if not DIGEST_PATTERN.fullmatch(args.digest):
        parser.error("--digest は sha256: に続けて、小文字の 16 進 64 桁にしてください")
    if not COMMIT_PATTERN.fullmatch(args.commit):
        parser.error("--commit は、小文字の 16 進 40 桁(省略形は不可)にしてください")
    if args.built_at is None:
        args.built_at = now_utc()
    elif not has_timezone(args.built_at):
        parser.error("--built-at は、タイムゾーンつきの ISO 8601(例: 2026-10-03T12:00:00Z)にしてください")
    return args


def _revoke(args: argparse.Namespace, table: dict, recorded: dict | None) -> int:
    if recorded is None:
        print(f"エラー: {args.revoke} は表にありません(失効させるものがありません)", file=sys.stderr)
        return 2
    if recorded["status"] == "revoked":
        print(f"already revoked: {args.revoke}(revoked_at {recorded.get('revoked_at')})。上書きしません")
        return 0
    recorded["status"] = "revoked"
    recorded["revoked_at"] = now_utc()
    write_table(args.releases, table)
    print(f"revoked: {args.revoke}(revoked_at {recorded['revoked_at']})→ {args.releases}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        table = read_table(args.releases)
    except ReleaseTableError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    digest = args.revoke or args.digest
    recorded = next((entry for entry in table["releases"] if entry["digest"] == digest), None)
    if args.revoke is not None:
        return _revoke(args, table, recorded)

    if recorded is not None:
        print(f"already recorded: {digest}(commit {recorded.get('commit')}、status {recorded['status']})。上書きしません")
        if recorded.get("commit") != args.commit:
            print(f"注意: 記録されているコミットと、渡されたコミット({args.commit})が違います", file=sys.stderr)
        return 0

    table["releases"].append({"digest": digest, "commit": args.commit, "built_at": args.built_at, "status": "active"})
    write_table(args.releases, table)
    print(f"recorded: {digest}(commit {args.commit}、{args.built_at}、status active)→ {args.releases}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
