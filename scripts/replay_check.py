"""リプレイの記録(JSONL)の検査と、記録の形・読み込み・再生の順序(design.md §8.4・§12.1 AC-21)。

    uv run python scripts/replay_check.py [PATH ...]    # PATH を省くと、fixtures/replays/case{1,2,3}.jsonl を検査する

終了コード: すべての記録が合格すれば 0、1 つでも不合格なら 1(理由を標準エラーに出す)。

記録の形(schema = replay/v1。scripts/run_demo.py の --record が書き、--replay が流す)
- 1 行目はヘッダ: {"header": true, "case": N, "source": "live"|"scripted", "recorded_at": ISO 8601, "schema": "replay/v1"}
- 2 行目以降は、1 行 1 イベント(見え方ごと): {"side": "candidate"|"employer", "seq": N, "observed_at": UNIX 秒(float),
  "event": <金庫の EventViewItem の JSON>}。observed_at は、記録する側が、そのイベントを読んだ時刻(金庫のモデルには時刻がない)。
  ファイルの順は、観測した順(同じ書き込みで同時に起きた双方のイベントは同じ時刻)。

検査するもの
- ヘッダの項目と値(schema・source・case・recorded_at)、各行の必須項目と型、event が EventViewItem の形であること
- observed_at が、ファイルの順に単調非減少であること
- seq が、側ごとに 1 からの連番であること(行の seq と event の seq が同じであること)
- 最後のイベントが最終結果(final_result)であること。最終結果は、双方に 1 件ずつで、各側の最後のイベントであり、中身(見込みと
  組み合わせ)が双方で同じであること(§3.1・AC-08)
- 3 回再生して(間隔を空けずに)、イベント列のハッシュが一致すること(AC-21)。ハッシュは observed_at を含まない
"""

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pydantic import ValidationError  # noqa: E402

from negotiation_core import Side  # noqa: E402
from vault.api_models import EventViewItem  # noqa: E402

REPLAY_SCHEMA = "replay/v1"
SOURCES = ("live", "scripted")
SIDES: tuple[Side, ...] = ("candidate", "employer")
REPLAYS_DIRECTORY = PROJECT_ROOT / "fixtures" / "replays"
DEFAULT_PATHS = tuple(REPLAYS_DIRECTORY / f"case{case}.jsonl" for case in (1, 2, 3))
REPLAY_TIMES = 3  # AC-21: 3 回再生して、イベント列のハッシュが一致すること

_HEADER_KEYS = frozenset({"header", "case", "source", "recorded_at", "schema"})
_LINE_KEYS = frozenset({"side", "seq", "observed_at", "event"})


@dataclasses.dataclass(frozen=True)
class ReplayEvent:
    """記録の 1 イベント: どちらの側の見え方か、記録する側が読んだ時刻(UNIX 秒)、金庫のイベント。"""

    side: Side
    observed_at: float
    item: EventViewItem

    @property
    def seq(self) -> int:
        return self.item.seq


@dataclasses.dataclass(frozen=True)
class Replay:
    """検査を通った記録。"""

    case: int
    source: str
    recorded_at: str
    events: tuple[ReplayEvent, ...]


# ----------------------------------------------------------------------
# 書き出し
# ----------------------------------------------------------------------


def dump_replay(case: int, source: str, recorded_at: str, events: Sequence[ReplayEvent]) -> str:
    """記録の本文(JSONL)を作る。1 行目がヘッダ、2 行目以降が 1 行 1 イベント。"""
    header = {"header": True, "case": case, "source": source, "recorded_at": recorded_at, "schema": REPLAY_SCHEMA}
    lines: list[dict[str, Any]] = [header]
    lines += [
        {"side": e.side, "seq": e.seq, "observed_at": e.observed_at, "event": e.item.model_dump(mode="json")}
        for e in events
    ]
    return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)


# ----------------------------------------------------------------------
# 読み込みと検査
# ----------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_time(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:  # float にできないほど大きな整数
        return False


def _load_object(raw: str, number: int, keys: frozenset[str], problems: list[str]) -> dict[str, Any] | None:
    """1 行を JSON のオブジェクトとして読み、項目が keys と過不足なく一致するか確かめる。ダメなら問題を足して None。"""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        problems.append(f"{number} 行目: JSON として読めません")
        return None
    if not isinstance(value, dict):
        problems.append(f"{number} 行目: JSON のオブジェクトではありません")
        return None
    missing, unknown = sorted(keys - value.keys()), sorted(value.keys() - keys)
    if missing:
        problems.append(f"{number} 行目: 必須の項目がありません: {', '.join(missing)}")
    if unknown:
        problems.append(f"{number} 行目: 知らない項目があります: {', '.join(unknown)}")
    return None if missing or unknown else value


def _check_header(header: dict[str, Any], problems: list[str]) -> None:
    if header["header"] is not True:
        problems.append("1 行目: header は true にしてください")
    if header["schema"] != REPLAY_SCHEMA:
        problems.append(f"1 行目: schema は {REPLAY_SCHEMA!r} にしてください")
    if header["source"] not in SOURCES:
        problems.append(f"1 行目: source は {' か '.join(SOURCES)} にしてください")
    if not _is_int(header["case"]) or header["case"] < 1:
        problems.append("1 行目: case は 1 以上の整数にしてください")
    try:
        if not isinstance(header["recorded_at"], str):
            raise ValueError
        dt.datetime.fromisoformat(header["recorded_at"])
    except ValueError:
        problems.append("1 行目: recorded_at は ISO 8601 の日時の文字列にしてください")


def _load_event(raw: object, entry_seq: object, number: int, problems: list[str]) -> EventViewItem | None:
    """event を EventViewItem として読む(行の seq と event の seq が同じことも確かめる)。"""
    if not isinstance(raw, dict):
        problems.append(f"{number} 行目: event がオブジェクトではありません")
        return None
    try:
        item = EventViewItem.model_validate(raw)
    except ValidationError as exc:
        where = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors()[:3])
        problems.append(f"{number} 行目: event が EventViewItem の形ではありません({where})")
        return None
    if raw.get("seq") != entry_seq:
        problems.append(f"{number} 行目: 行の seq と event の seq が違います")
        return None
    return item


def _check_final_results(events: Sequence[ReplayEvent], problems: list[str]) -> None:
    """最後のイベントが最終結果で、最終結果が双方に 1 件ずつ・各側の最後・同じ中身であること。"""
    if events[-1].item.kind != "final_result":
        problems.append("最後のイベントが最終結果(final_result)ではありません")
    results = {}
    for side in SIDES:
        side_events = [e.item for e in events if e.side == side]
        finals = [item for item in side_events if item.kind == "final_result"]
        if not side_events:
            problems.append(f"{side} 側のイベントがありません")
        elif len(finals) != 1 or side_events[-1].kind != "final_result":
            problems.append(f"{side} 側の最終結果(final_result)は、最後のイベントとして 1 件だけにしてください")
        elif finals[0].result is None:
            problems.append(f"{side} 側の最終結果に result がありません")
        else:
            results[side] = finals[0].result
    if len(results) == len(SIDES) and results["candidate"] != results["employer"]:
        problems.append("最終結果の中身が、候補者側と求人側で違います")


def parse_replay(text: str) -> tuple[Replay | None, list[str]]:
    """記録の本文を読んで検査する。問題がなければ (Replay, [])、あれば (None, 問題の一覧)。ファイルの外のものは見ない。"""
    problems: list[str] = []
    lines = text.splitlines()
    if not lines:
        return None, ["記録が空です(ヘッダの行がありません)"]

    header = _load_object(lines[0], 1, _HEADER_KEYS, problems)
    if header is not None:
        _check_header(header, problems)

    events: list[ReplayEvent] = []
    next_seq: dict[str, int] = {side: 1 for side in SIDES}
    previous_time: float | None = None
    for number, raw in enumerate(lines[1:], start=2):
        entry = _load_object(raw, number, _LINE_KEYS, problems)
        if entry is None:
            continue
        side, seq, observed_at = entry["side"], entry["seq"], entry["observed_at"]
        if side not in SIDES:
            problems.append(f"{number} 行目: side は {' か '.join(SIDES)} にしてください")
            continue
        if not _is_int(seq):
            problems.append(f"{number} 行目: seq は整数にしてください")
            continue
        if not _is_time(observed_at):
            problems.append(f"{number} 行目: observed_at は 0 以上の数(UNIX 秒)にしてください")
            continue
        item = _load_event(entry["event"], seq, number, problems)
        if item is None:
            continue
        if seq != next_seq[side]:
            problems.append(f"{number} 行目: {side} 側の seq が連番ではありません(期待 {next_seq[side]}、実際 {seq})")
        next_seq[side] = seq + 1
        if previous_time is not None and observed_at < previous_time:
            problems.append(f"{number} 行目: observed_at が前の行より小さくなっています")
        previous_time = observed_at
        events.append(ReplayEvent(side, float(observed_at), item))

    if not events:
        problems.append("イベントの行がありません")
    else:
        _check_final_results(events, problems)
    if problems or header is None:
        return None, problems
    return Replay(header["case"], header["source"], header["recorded_at"], tuple(events)), []


def events_digest(events: Sequence[ReplayEvent]) -> str:
    """イベント列のハッシュ(side・seq・event の中身。observed_at は含まない)。"""
    material = [[e.side, e.seq, e.item.model_dump(mode="json")] for e in events]
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# 再生
# ----------------------------------------------------------------------


def iter_replay(
    replay: Replay, speed: float = 1.0, sleep: Callable[[float], None] = time.sleep
) -> Iterator[ReplayEvent]:
    """記録のイベントを順に渡す。前のイベントとの observed_at の差を、speed で割った秒数だけ、sleep してから渡す(最初は待たない)。

    speed が大きいほど速い。math.inf なら待たない(AC-21 の 3 回再生。テスト)。
    """
    previous: float | None = None
    for event in replay.events:
        if previous is not None:
            wait = (event.observed_at - previous) / speed
            if wait > 0:
                sleep(wait)
        previous = event.observed_at
        yield event


def replay_digests(replay: Replay, times: int = REPLAY_TIMES) -> list[str]:
    """間隔を空けずに times 回再生して、毎回のイベント列のハッシュを返す。"""
    return [events_digest(list(iter_replay(replay, math.inf))) for _ in range(times)]


# ----------------------------------------------------------------------
# コマンド
# ----------------------------------------------------------------------


def check_file(path: Path) -> tuple[Replay | None, list[str]]:
    """記録のファイルを読んで検査し、3 回再生してハッシュを比べる(AC-21)。問題がなければ (Replay, [])。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"読めません({type(exc).__name__})"]
    except UnicodeDecodeError:
        return None, ["UTF-8 として読めません"]
    replay, problems = parse_replay(text)
    if replay is None:
        return None, problems
    digests = replay_digests(replay)
    if len(set(digests)) != 1 or digests[0] != events_digest(replay.events):
        return None, [f"{REPLAY_TIMES} 回再生したイベント列のハッシュが一致しません"]
    return replay, []


def display_path(path: Path) -> str:
    """表示用のパス(プロジェクトの中なら、プロジェクト直下からの相対)。"""
    resolved = path.resolve()
    return str(resolved.relative_to(PROJECT_ROOT)) if resolved.is_relative_to(PROJECT_ROOT) else str(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="リプレイの記録(JSONL)の形と、3 回再生したときのイベント列の一致を検査する。")
    parser.add_argument("paths", nargs="*", type=Path, help="検査する記録。省くと fixtures/replays/case{1,2,3}.jsonl")
    args = parser.parse_args(argv)

    failed = 0
    for path in args.paths or DEFAULT_PATHS:
        replay, problems = check_file(path)
        if replay is None:
            failed += 1
            print(f"NG {display_path(path)}", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            continue
        counts = {side: sum(1 for e in replay.events if e.side == side) for side in SIDES}
        print(
            f"OK {display_path(path)}: ケース {replay.case}・{replay.source}・イベント {len(replay.events)} 件"
            f"(候補者側 {counts['candidate']}・求人側 {counts['employer']})"
            f"・{REPLAY_TIMES} 回再生してハッシュが一致({events_digest(replay.events)[:12]})"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
