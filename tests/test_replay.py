"""リプレイ(design.md §8.4・§12.1 AC-09〜11・AC-21。scripts/run_demo.py の --record・--replay と scripts/replay_check.py)。

本物の Gemini・GCP は使わない(台本のエージェントと、Firestore エミュレータだけ)。
- 記録 → 検査 → 再生の往復: `run_demo --case N --record PATH` が書いた記録が replay_check に通り、`--replay PATH`(PATH を省いて
  `--case N --replay` でもよい。AC-09〜11 の書き方)が同じイベントを順に流す(先頭に「リプレイ」)。3 つのケースとも。
  記録の形(ヘッダ・1 行 1 イベント・observed_at)は、仕様どおりの項目だけを持つ。
- 記録の中身: 金庫のイベント列(見え方ごと)と同じで、observed_at は単調非減少、seq は側ごとに連番、最後は最終結果。
- 壊した記録は replay_check が 1 を返し、理由を出す。--replay は、壊れた記録・無い記録を流さない。
- 再生の間隔: 記録どおりの間隔を --speed で割った秒数だけ待つ。倍率が無限大なら待たない。
- AC-21: 3 回再生して、イベント列のハッシュが一致する。ハッシュは、イベントの中身が変われば変わり、時刻には左右されない。
- --record は、検査に通らない記録(判定に届かなかった実行など)を書かず、終了コード 1 を返す。
- fixtures/replays/case{1,2,3}.jsonl: 検査に通り、ケースの意図どおりの結果で、台本の記録は、いまの台本の実行と同じイベント列。
"""

import asyncio
import contextlib
import dataclasses
import json
import math
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.cloud import firestore
from negotiation_core.estimate_interval import estimate_interval

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))
import replay_check  # noqa: E402  (scripts/ を import できるようにしてから読む)
import run_demo as demo_script  # noqa: E402

from scripted_negotiators import is_on_search_line  # noqa: E402
from vault.api_models import EventViewItem  # noqa: E402
from vault.fixtures import load_case_fixture  # noqa: E402

CASES = (1, 2, 3)
COMMITTED_REPLAYS = {case: replay_check.REPLAYS_DIRECTORY / f"case{case}.jsonl" for case in CASES}
ID_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")  # 16 桁の 16 進数(交渉 ID・依頼者 ID の形。§2.7)


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_lines(path: Path, lines: list[dict]) -> Path:
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def recordings(firestore_emulator_host, tmp_path_factory) -> dict[int, Path]:
    """ケース 1〜3 を、台本のエージェントで `--record` つきで 1 回ずつ動かして書いた記録 {ケース: パス}(このファイルで共有する)。"""
    directory = tmp_path_factory.mktemp("replays")

    @contextlib.contextmanager
    def session_emulator():
        yield firestore_emulator_host

    paths = {case: directory / f"case{case}.jsonl" for case in CASES}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(demo_script, "firestore_emulator", session_emulator)
        patch.setattr(demo_script, "RUN_RECORD_DIRECTORY", directory / "runs")
        for case, path in paths.items():
            assert demo_script.main(["--case", str(case), "--record", str(path)]) == 0
    return paths


@pytest.fixture(scope="module")
def recorded_run(firestore_emulator_host):
    """ケース 1 を台本のエージェントで動かし、イベントを観測した DemoRun(record_events=True)。"""
    project = f"demo-test-{uuid.uuid4().hex}"
    vault_db = firestore.Client(project=project, database="vault-db")
    default_db = firestore.Client(project=project, database="(default)")
    try:
        return asyncio.run(
            demo_script.run_demo(
                fixture=load_case_fixture(1), mode="scripted", vault_db=vault_db, default_db=default_db, record_events=True
            )
        )
    finally:
        vault_db.close()
        default_db.close()


def load_replay(path: Path) -> replay_check.Replay:
    replay, problems = replay_check.check_file(path)
    assert replay is not None, problems
    return replay


def candidate_probes(replay: replay_check.Replay):
    """候補者側の金庫の答え: 相手(攻撃者)の提案を受けたときの評価(受け手としての評価)。"""
    return [e.item for e in replay.events if e.side == "candidate" and e.item.kind == "offer_received"]


# ----------------------------------------------------------------------
# 記録 → 検査 → 再生
# ----------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_a_recording_has_exactly_the_specified_shape(recordings, case):
    # 仕様(§8.4): 先頭にヘッダ {"header", "case", "source", "recorded_at", "schema"}、あとは 1 行 1 イベント
    # {"side", "seq", "observed_at"(UNIX 秒の float), "event"(EventViewItem の JSON)}。
    lines = read_lines(recordings[case])
    header, events = lines[0], lines[1:]
    assert set(header) == {"header", "case", "source", "recorded_at", "schema"}
    assert (header["header"], header["case"], header["source"], header["schema"]) == (True, case, "scripted", "replay/v1")
    assert isinstance(header["recorded_at"], str) and header["recorded_at"].endswith("+00:00")
    assert events and all(set(line) == {"side", "seq", "observed_at", "event"} for line in events)
    assert all(isinstance(line["observed_at"], float) and line["observed_at"] > 1_700_000_000 for line in events)
    times = [line["observed_at"] for line in events]
    assert times == sorted(times)  # 単調非減少
    assert all(line["event"]["seq"] == line["seq"] for line in events)
    assert {line["side"] for line in events} == {"candidate", "employer"}
    assert events[-1]["event"]["kind"] == "final_result"
    assert not ID_IN_TEXT.search(recordings[case].read_text(encoding="utf-8"))  # 交渉 ID・依頼者 ID は書かない


def test_the_observer_collects_the_vault_events_of_both_sides_in_the_order_they_were_written(recorded_run):
    # 観測は、金庫のイベント列(見え方ごと)そのもの: 側ごとに 1 から連番で、最後に金庫から読んだものと同じ。時刻は単調非減少。
    run = recorded_run
    for side, items in run.events.items():
        assert [e.item for e in run.observed if e.side == side] == items
        assert [e.seq for e in run.observed if e.side == side] == list(range(1, len(items) + 1))
    times = [e.observed_at for e in run.observed]
    assert times == sorted(times) and times[0] > 1_700_000_000
    # 1 回の書き込みで同時に起きる双方のイベントは同じ時刻で、書いた側が先(最初の手: 候補者の確かめ → 提案 → 求人の受信)
    assert [(e.side, e.item.kind) for e in run.observed[:3]] == [
        ("candidate", "check"),
        ("candidate", "propose"),
        ("employer", "offer_received"),
    ]
    assert run.observed[1].observed_at == run.observed[2].observed_at
    # 最後は、書いた側(合意を受けた側)が先の、双方の最終結果
    assert [e.item.kind for e in run.observed[-2:]] == ["final_result", "final_result"]
    assert run.observed[-2].observed_at == run.observed[-1].observed_at


@pytest.mark.parametrize("case", CASES)
def test_a_recording_passes_the_check_and_is_replayed_with_the_replay_notice_first(recordings, case, capsys):
    assert replay_check.main([str(recordings[case])]) == 0
    checked = capsys.readouterr().out
    assert checked.startswith("OK ") and f"ケース {case}・scripted" in checked and "3 回再生してハッシュが一致" in checked

    assert demo_script.main(["--replay", str(recordings[case]), "--speed", "1e9"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("run_demo: リプレイ(") and f"ケース {case}" in out.splitlines()[0]
    assert "金庫・agents・Gemini は呼ばない" in out

    # 記録のイベントが、すべて、記録の順に、live の報告と同じ形で流れる
    replay = load_replay(recordings[case])
    streamed = [line.split("] ", 1)[1].strip() for line in out.splitlines() if "[候補者側]" in line or "[求人側]" in line]
    assert streamed == [demo_script._format_event(e.item).strip() for e in replay.events]
    result = replay.events[-1].item.result
    assert f"見込み: {demo_script._LIKELIHOOD_LABELS[result.likelihood]}({result.likelihood})" in out
    assert out.rstrip().endswith(f"組み合わせ: {demo_script.format_package(result.package)}")


def test_case_1_is_recorded_as_an_agreement_both_people_accept(recordings):
    replay = load_replay(recordings[1])
    result = replay.events[-1].item.result
    fixture = load_case_fixture(1)
    assert result.likelihood in ("high", "medium")
    assert fixture.candidate.raw.accepts(result.package) and fixture.employer.rules[0].raw.accepts(result.package)


def test_case_2_is_recorded_as_none_for_both_sides(recordings):
    replay = load_replay(recordings[2])
    finals = [e.item for e in replay.events if e.item.kind == "final_result"]
    assert len(finals) == 2
    assert all((f.result.likelihood, f.result.package) == ("none", None) for f in finals)


def test_case_3_is_recorded_with_the_answers_that_narrow_the_salary_to_one_grid_cell(recordings):
    probes = candidate_probes(load_replay(recordings[3]))
    assert all(is_on_search_line(item.package) for item in probes)
    interval = estimate_interval([(item.package.salary, item.own_evaluation) for item in probes])
    assert (interval.lower, interval.upper, interval.cells) == (600, 650, 1)
    assert interval.contains(load_case_fixture(3).candidate.raw.bounds[(1, 0, 6)])  # 生の境目 620 万を含む


def test_a_recording_of_a_live_run_says_so_in_the_header(recorded_run, tmp_path):
    path = tmp_path / "live.jsonl"
    assert demo_script.record_replay(dataclasses.replace(recorded_run, mode="live"), path) == []
    assert read_lines(path)[0]["source"] == "live"
    assert replay_check.main([str(path)]) == 0


def test_the_replay_command_plays_the_committed_replay_of_the_case_when_given_only_the_case(capsys):
    # AC-09〜11 の書き方: `run_demo.py --case N --replay`。PATH を省くと、そのケースの fixtures/replays/case{N}.jsonl
    assert demo_script.main(["--case", "2", "--replay", "--speed", "1e9"]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("run_demo: リプレイ(") and "ケース 2" in out.splitlines()[0]
    streamed = [line.split("] ", 1)[1].strip() for line in out.splitlines() if "[候補者側]" in line or "[求人側]" in line]
    assert streamed == [demo_script._format_event(e.item).strip() for e in load_replay(COMMITTED_REPLAYS[2]).events]


def test_the_replay_command_checks_that_the_recording_is_of_the_case_asked_for(recordings, capsys):
    assert demo_script.main(["--case", "1", "--replay", str(recordings[1]), "--speed", "1e9"]) == 0
    assert "ケース 1" in capsys.readouterr().out.splitlines()[0]

    assert demo_script.main(["--case", "2", "--replay", str(recordings[1]), "--speed", "1e9"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "記録はケース 1 で、--case 2 と違います" in captured.err


def test_replay_output_has_no_id(recordings, capsys):
    assert demo_script.main(["--replay", str(recordings[3]), "--speed", "1e9"]) == 0
    assert not ID_IN_TEXT.search(capsys.readouterr().out)


class FakeVault:
    """金庫クライアントの代わり。書き換える操作のたびに、側ごとのイベントが増える(スクリプトの実行が通らない経路の確かめ用)。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: dict[str, list[EventViewItem]] = {"candidate": [], "employer": []}
        self.fail_next_write = False

    def _add(self, side: str, kind: str) -> None:
        self.events[side].append(EventViewItem(seq=len(self.events[side]) + 1, kind=kind))

    def _write(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("conflict")

    async def get_view(self, nid, side):
        self.calls.append("get_view")
        return "view"

    async def get_events(self, nid, side, after_seq=0):
        self.calls.append(f"get_events:{side}:{after_seq}")
        return [item for item in self.events[side] if item.seq > after_seq]

    async def post_move(self, nid, request):
        self._write("post_move")
        self._add(request.side, "propose")
        self._add("employer" if request.side == "candidate" else "candidate", "offer_received")

    async def post_principal_answer(self, nid, request):
        self._write("post_principal_answer")
        self._add(request.side, "principal_answer")

    async def stop_cost_limit(self, nid):
        self._write("stop_cost_limit")
        self._add("candidate", "final_result")
        self._add("employer", "final_result")


@pytest.mark.anyio
async def test_the_observing_vault_observes_after_every_write_with_the_writing_side_first_and_never_after_a_read():
    fake = FakeVault()
    clock = iter([100.0, 99.0, 105.0, 105.5]).__next__  # 2 回目は、時計が戻った
    observer = demo_script.EventObserver(fake, "0123456789abcdef", clock)
    vault = demo_script.ObservingVault(fake, observer)

    assert await vault.get_view("0123456789abcdef", "candidate") == "view"  # 読み出しは素通し。観測しない
    assert observer.events == []

    await vault.post_move("0123456789abcdef", SimpleNamespace(side="employer"))  # 書いた側(求人)のイベントが先
    await vault.post_principal_answer("0123456789abcdef", SimpleNamespace(side="candidate"))
    await vault.stop_cost_limit("0123456789abcdef")  # 側を持たない操作は、候補者側が先

    assert [(e.side, e.item.kind, e.seq) for e in observer.events] == [
        ("employer", "propose", 1),
        ("candidate", "offer_received", 1),
        ("candidate", "principal_answer", 2),
        ("candidate", "final_result", 3),
        ("employer", "final_result", 2),
    ]
    assert [e.observed_at for e in observer.events] == [100.0, 100.0, 100.0, 105.0, 105.0]  # 戻らない。同じ回は同じ時刻
    # 各側の after_seq 以降だけを読む(同じイベントを二重に集めない)
    assert "get_events:candidate:1" in fake.calls and "get_events:employer:1" in fake.calls
    assert fake.calls[0] == "get_view" and fake.calls[1] == "post_move"


@pytest.mark.anyio
async def test_the_observing_vault_does_not_observe_a_write_that_failed():
    fake = FakeVault()
    observer = demo_script.EventObserver(fake, "0123456789abcdef", lambda: 100.0)
    vault = demo_script.ObservingVault(fake, observer)
    fake.fail_next_write = True

    with pytest.raises(RuntimeError, match="conflict"):
        await vault.post_move("0123456789abcdef", SimpleNamespace(side="candidate"))

    assert observer.events == [] and fake.calls == ["post_move"]  # 失敗した書き込みのあとに、読みに行かない


# ----------------------------------------------------------------------
# 壊した記録
# ----------------------------------------------------------------------


def _text(lines: list[dict]) -> str:
    return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)


def _with(lines: list[dict], index: int, **changes) -> list[dict]:
    lines[index] = {**lines[index], **changes}
    return lines


def _without(lines: list[dict], index: int, key: str) -> list[dict]:
    lines[index] = {name: value for name, value in lines[index].items() if name != key}
    return lines


def _final_result_lines(lines: list[dict]) -> list[int]:
    return [i for i, line in enumerate(lines) if i > 0 and line["event"]["kind"] == "final_result"]


def _results_differ(lines: list[dict]) -> list[dict]:
    last = _final_result_lines(lines)[-1]
    lines[last] = {**lines[last], "event": {**lines[last]["event"], "result": {"likelihood": "none", "package": None}}}
    return lines


# 事前に確かめた 1 行目=ヘッダ、2 行目=候補者の seq 1、3 行目=候補者の seq 2、4 行目=求人の seq 1 の並びを使う(ケース 1 の記録)
BROKEN_RECORDINGS = [
    pytest.param(lambda lines: "", "記録が空です", id="empty"),
    pytest.param(lambda lines: lines[1:], "1 行目: 必須の項目がありません", id="no_header"),
    pytest.param(lambda lines: lines[:1], "イベントの行がありません", id="no_events"),
    pytest.param(lambda lines: _with(lines, 0, schema="replay/v2"), "schema は 'replay/v1'", id="wrong_schema"),
    pytest.param(lambda lines: _with(lines, 0, source="recorded"), "source は live か scripted", id="unknown_source"),
    pytest.param(lambda lines: _with(lines, 0, case=0), "case は 1 以上の整数", id="case_zero"),
    pytest.param(lambda lines: _with(lines, 0, case=True), "case は 1 以上の整数", id="case_is_a_bool"),
    pytest.param(lambda lines: _with(lines, 0, header=False), "header は true", id="header_flag_false"),
    pytest.param(lambda lines: _without(lines, 0, "recorded_at"), "必須の項目がありません: recorded_at", id="no_recorded_at"),
    pytest.param(lambda lines: _with(lines, 0, recorded_at="yesterday"), "recorded_at は ISO 8601", id="bad_recorded_at"),
    pytest.param(lambda lines: _text(lines) + "{oops\n", "JSON として読めません", id="not_json"),
    pytest.param(lambda lines: _text(lines[:3]) + "\n" + _text(lines[3:]), "JSON として読めません", id="blank_line"),
    pytest.param(lambda lines: _without(lines, 3, "observed_at"), "必須の項目がありません: observed_at", id="no_observed_at"),
    pytest.param(lambda lines: _with(lines, 3, note="x"), "知らない項目があります: note", id="extra_key"),
    pytest.param(lambda lines: _with(lines, 3, side="buyer"), "side は candidate か employer", id="unknown_side"),
    pytest.param(lambda lines: _with(lines, 3, observed_at=-1.0), "observed_at は 0 以上の数", id="observed_at_negative"),
    pytest.param(lambda lines: _with(lines, 3, observed_at="now"), "observed_at は 0 以上の数", id="observed_at_text"),
    pytest.param(lambda lines: _with(lines, 3, observed_at=10**400), "observed_at は 0 以上の数", id="observed_at_too_large"),
    pytest.param(
        lambda lines: _with(lines, 4, observed_at=lines[3]["observed_at"] - 1), "observed_at が前の行より小さく", id="time_goes_back"
    ),
    pytest.param(lambda lines: lines[:3] + lines[4:], "連番ではありません", id="seq_gap"),
    pytest.param(lambda lines: lines[:3] + [lines[2]] + lines[3:], "連番ではありません", id="seq_duplicated"),
    pytest.param(lambda lines: _with(lines, 3, seq=99), "行の seq と event の seq が違います", id="line_seq_differs_from_event_seq"),
    pytest.param(lambda lines: _with(lines, 3, seq="1"), "seq は整数", id="seq_is_text"),
    pytest.param(lambda lines: _with(lines, 3, event="text"), "event がオブジェクトではありません", id="event_is_not_an_object"),
    pytest.param(
        lambda lines: _with(lines, 3, event={**lines[3]["event"], "note": 1}), "EventViewItem の形ではありません", id="event_extra_key"
    ),
    pytest.param(
        lambda lines: _with(lines, 3, event={**lines[3]["event"], "kind": "accept"}),
        "EventViewItem の形ではありません",
        id="event_unknown_kind",
    ),
    pytest.param(lambda lines: lines[:-2], "最後のイベントが最終結果(final_result)ではありません", id="last_event_is_not_the_final_result"),
    pytest.param(
        lambda lines: lines[:-1], "最終結果(final_result)は、最後のイベントとして 1 件だけ", id="one_side_has_no_final_result"
    ),
    pytest.param(
        lambda lines: [line for line in lines if line.get("side") != "employer"], "employer 側のイベントがありません", id="one_side_only"
    ),
    pytest.param(_results_differ, "最終結果の中身が、候補者側と求人側で違います", id="final_results_differ"),
]


@pytest.mark.parametrize(("break_it", "expected"), BROKEN_RECORDINGS)
def test_a_broken_recording_fails_the_check_with_the_reason(recordings, tmp_path, capsys, break_it, expected):
    broken = break_it(read_lines(recordings[1]))
    path = tmp_path / "broken.jsonl"
    path.write_text(broken if isinstance(broken, str) else _text(broken), encoding="utf-8")

    assert replay_check.main([str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.err.startswith("NG ") and expected in captured.err
    assert captured.out == ""


def test_the_check_fails_when_any_one_of_several_recordings_is_broken(recordings, tmp_path, capsys):
    broken = write_lines(tmp_path / "broken.jsonl", read_lines(recordings[1])[:-1])

    assert replay_check.main([str(recordings[2]), str(broken), str(recordings[3])]) == 1

    captured = capsys.readouterr()
    assert captured.out.count("OK ") == 2 and captured.err.count("NG ") == 1


def test_the_check_fails_for_a_missing_file_and_a_file_that_is_not_utf8(tmp_path, capsys):
    (tmp_path / "binary.jsonl").write_bytes(b"\xff\xfe\x00")

    assert replay_check.main([str(tmp_path / "missing.jsonl"), str(tmp_path / "binary.jsonl")]) == 1

    err = capsys.readouterr().err
    assert "読めません(FileNotFoundError)" in err and "UTF-8 として読めません" in err


def test_the_replay_command_does_not_play_a_broken_or_missing_recording(recordings, tmp_path, capsys):
    broken = write_lines(tmp_path / "broken.jsonl", read_lines(recordings[1])[:-2])  # 最終結果がない

    for path in (broken, tmp_path / "missing.jsonl"):
        assert demo_script.main(["--replay", str(path), "--speed", "1e9"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""  # 「リプレイ」の表示も、イベントも出ない
    assert captured.err.count("リプレイを始められません") == 2 and "最後のイベントが最終結果" in captured.err


# ----------------------------------------------------------------------
# 再生の間隔
# ----------------------------------------------------------------------


def test_the_replay_waits_the_recorded_gaps_divided_by_the_speed(recordings, tmp_path):
    # 記録の時刻を、1 つ目と 2 つ目を同時に、あとは 0.5 秒おきに直して、倍率ごとの待ち時間を見る。最初のイベントの前には待たない。
    lines = read_lines(recordings[1])
    for number, line in enumerate(lines[1:]):
        line["observed_at"] = 1_800_000_000.0 + 0.5 * max(number - 1, 0)
    path = write_lines(tmp_path / "paced.jsonl", lines)
    replay = load_replay(path)
    gaps = len(replay.events) - 2  # 同時の 1 組の間は待たない

    for speed in (1.0, 4.0, math.inf):
        waits: list[float] = []
        assert list(replay_check.iter_replay(replay, speed, waits.append)) == list(replay.events)
        assert waits == ([] if speed == math.inf else [0.5 / speed] * gaps)

    waits = []
    assert demo_script.play_replay(path, 2.0, sleep=waits.append) == 0  # コマンドも同じ待ち方をする
    assert waits == [0.25] * gaps


# ----------------------------------------------------------------------
# AC-21: 3 回再生して、イベント列のハッシュが一致する
# ----------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_replaying_three_times_gives_the_same_event_hash(recordings, case):
    replay = load_replay(recordings[case])

    digests = replay_check.replay_digests(replay)

    assert len(digests) == replay_check.REPLAY_TIMES == 3
    assert len(set(digests)) == 1 and digests[0] == replay_check.events_digest(replay.events)


def test_the_event_hash_follows_the_events_and_ignores_the_times(recordings):
    replay = load_replay(recordings[1])
    events = list(replay.events)
    base = replay_check.events_digest(events)

    later = [dataclasses.replace(e, observed_at=e.observed_at + 100) for e in events]
    assert replay_check.events_digest(later) == base  # 時刻には左右されない

    reordered = [events[1], events[0], *events[2:]]
    assert replay_check.events_digest(reordered) != base  # 並びが変われば変わる
    other_event = dataclasses.replace(events[0], item=events[0].item.model_copy(update={"kind": "propose"}))
    assert replay_check.events_digest([other_event, *events[1:]]) != base  # 中身が変われば変わる
    assert replay_check.events_digest(events[:-1]) != base


# ----------------------------------------------------------------------
# --record は、検査に通らない記録を書かない
# ----------------------------------------------------------------------


def test_record_replay_writes_nothing_when_the_observed_events_miss_the_end(recorded_run, tmp_path):
    path = tmp_path / "unfinished.jsonl"
    unfinished = dataclasses.replace(recorded_run, observed=recorded_run.observed[:-2])  # 最終結果を観測し損ねた

    problems = demo_script.record_replay(unfinished, path)

    assert problems and any("食い違っている" in problem for problem in problems)
    assert any("最終結果" in problem for problem in problems)
    assert not path.exists()


def test_record_replay_writes_nothing_for_a_run_that_did_not_record_events(recorded_run, tmp_path):
    path = tmp_path / "not_observed.jsonl"

    problems = demo_script.record_replay(dataclasses.replace(recorded_run, observed=[]), path)

    assert problems and not path.exists()


def test_main_returns_one_and_writes_no_file_when_the_recording_cannot_be_written(
    monkeypatch, recorded_run, tmp_path, firestore_emulator_host, capsys
):
    @contextlib.contextmanager
    def session_emulator():
        yield firestore_emulator_host

    async def unfinished_run(**kwargs):
        assert kwargs["record_events"] is True  # --record を付けたときだけ、イベントを観測する
        return dataclasses.replace(recorded_run, observed=recorded_run.observed[:-2])

    monkeypatch.setattr(demo_script, "firestore_emulator", session_emulator)
    monkeypatch.setattr(demo_script, "RUN_RECORD_DIRECTORY", tmp_path / "runs")
    monkeypatch.setattr(demo_script, "run_demo", unfinished_run)
    path = tmp_path / "unfinished.jsonl"

    assert demo_script.main(["--case", "1", "--record", str(path)]) == 1

    captured = capsys.readouterr()
    assert "リプレイの記録を書かなかった" in captured.err and "リプレイの記録を書けなかったので、終了コード 1" in captured.out
    assert not path.exists()


def test_main_observes_no_events_unless_a_recording_is_asked_for(monkeypatch, recorded_run, tmp_path, firestore_emulator_host):
    @contextlib.contextmanager
    def session_emulator():
        yield firestore_emulator_host

    async def run(**kwargs):
        assert kwargs["record_events"] is False
        return dataclasses.replace(recorded_run, observed=[])

    monkeypatch.setattr(demo_script, "firestore_emulator", session_emulator)
    monkeypatch.setattr(demo_script, "RUN_RECORD_DIRECTORY", tmp_path / "runs")
    monkeypatch.setattr(demo_script, "run_demo", run)

    assert demo_script.main(["--case", "1"]) == 0


# ----------------------------------------------------------------------
# fixtures/replays/case{1,2,3}.jsonl
# ----------------------------------------------------------------------


def test_the_check_without_arguments_checks_the_three_committed_replays(capsys):
    # AC-21 のコマンド: `uv run python scripts/replay_check.py`
    assert replay_check.main([]) == 0

    out = capsys.readouterr().out
    assert [line.split(":")[0] for line in out.splitlines()] == [
        f"OK fixtures/replays/case{case}.jsonl" for case in CASES
    ]


@pytest.mark.parametrize("case", CASES)
def test_a_committed_replay_belongs_to_its_case_and_ends_the_way_the_case_intends(case):
    replay = load_replay(COMMITTED_REPLAYS[case])
    fixture = load_case_fixture(case)

    assert replay.case == case
    result = replay.events[-1].item.result
    if case == 1:  # AC-09: 「中」以上で組み合わせ 1 つ。両者が受けられる
        assert result.likelihood in ("high", "medium")
        assert fixture.candidate.raw.accepts(result.package) and fixture.employer.rules[0].raw.accepts(result.package)
    if case == 2:  # AC-10: 双方に「なし」だけ
        assert (result.likelihood, result.package) == ("none", None)
        assert [e.item.result for e in replay.events if e.item.kind == "final_result"] == [result, result]


@pytest.mark.parametrize("case", CASES)
def test_a_committed_scripted_replay_is_the_same_event_list_as_a_fresh_scripted_run(recordings, case):
    # 台本の記録は、いまの台本のエージェントとフィクスチャの実行と同じイベント列(フィクスチャやエージェントを変えたら、
    # `uv run python scripts/run_demo.py --case N --record fixtures/replays/caseN.jsonl` で取り直す)。
    # 本物の Gemini の記録(source=live)は、実行のたびに道筋が変わるので比べない(形の検査と、ケースの結果は上のテスト)。
    committed = load_replay(COMMITTED_REPLAYS[case])
    if committed.source != "scripted":
        pytest.skip("a live recording is not reproducible")
    fresh = load_replay(recordings[case])
    assert [(e.side, e.item) for e in committed.events] == [(e.side, e.item) for e in fresh.events]


def test_the_committed_case_3_replay_shows_the_meter_stopping_at_one_grid_cell():
    replay = load_replay(COMMITTED_REPLAYS[3])
    if replay.source != "scripted":
        pytest.skip("a live attack stops where the attacker agent stops")
    interval = estimate_interval([(i.package.salary, i.own_evaluation) for i in candidate_probes(replay)])
    assert (interval.lower, interval.upper) == (600, 650)
