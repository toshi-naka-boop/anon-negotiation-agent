"""scripts/canary_scan.py(design.md §12.1 の AC-02・AC-17・AC-18)の試験。

スクリプトは、別のプロセスで動かす(プロセスで 1 回しか設定できない OpenTelemetry の TracerProvider を、テスト全体に残さないため。
tests/test_interview_agent.py のスパンの試験と同じ理由)。Firestore エミュレータは、pytest が起動しているもの(FIRESTORE_EMULATOR_HOST)を、
スクリプトが使う(スクリプト自身が起動するのは、環境変数がないとき)。LLM はスタブで、本物の Gemini にも GCP にも接続しない。

確かめること
- 何も置かなければ、終了コード 0。調べた量(文書・行・件)が場所ごとに出て、どれも 0 でない(空だから通るのではない)。
- カナリアを故意に Firestore に書くと、終了コード 1(vault-db と (default) のどちらでも)。ログ・メモリ・スパンに置いても、1。
  置いた場所だけが NG になる(ほかの場所を巻き込まない)。
- 本物のスパン(ADK の call_llm)の対照: ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true にすると、面談の原文がスパンに載り、スパンの検査が NG になる
  (置いたカナリアだけでなく、本物の中身も見つけられる)。
- 探す部品: bytes の項目は除く、サブコレクションの下の文書も探す、調べた量が 0 なら通らない。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "canary_scan.py"
sys.path.insert(0, str(ROOT / "scripts"))
import canary_scan as scan_script  # noqa: E402  (scripts/ を import できるようにしてから読む)

PLACES = ("vault-db", "(default)", "ログ", "メモリ(面談の状態)", "スパン")


def run_scan(emulator_host: str, *args: str, **environment: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "FIRESTORE_EMULATOR_HOST": emulator_host, **environment}
    if "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS" not in environment:
        env.pop("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", None)  # 本番の既定(面談のモジュールが false にする)で動かす
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def lines_by_place(result: subprocess.CompletedProcess) -> dict[str, str]:
    """場所 → その行([OK]・[NG] で始まる行)。"""
    found = {}
    for line in result.stdout.splitlines():
        for place in PLACES:
            if line.startswith(f"[OK] {place}:") or line.startswith(f"[NG] {place}:"):
                found[place] = line
    return found


def scanned_amount(line: str) -> int:
    """「[OK] ログ: 28 行を調べた。…」の 28。"""
    return int(re.search(r": (\d+) ", line).group(1))


def test_the_scan_passes_and_shows_how_much_it_looked_at(firestore_emulator_host):
    result = run_scan(firestore_emulator_host)

    assert result.returncode == 0, result.stdout + result.stderr
    lines = lines_by_place(result)
    assert set(lines) == set(PLACES)
    assert all(line.startswith("[OK]") for line in lines.values()), result.stdout
    for place in ("vault-db", "(default)", "ログ", "スパン"):
        assert scanned_amount(lines[place]) > 0, lines[place]  # 空だから通ったのではない
    assert "文書を調べた" in lines["vault-db"] and "行を調べた" in lines["ログ"] and "件を調べた" in lines["スパン"]
    assert "送信の前の状態 1 件" in lines["メモリ(面談の状態)"] and "送信のあとの状態(0 件のはず)0 件" in lines["メモリ(面談の状態)"]
    assert "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false" in lines["スパン"]
    assert "終了コード 0" in result.stdout


def test_a_canary_written_to_the_vault_db_makes_the_scan_fail(firestore_emulator_host):
    result = run_scan(firestore_emulator_host, "--plant", "vault-db")

    assert result.returncode == 1, result.stdout + result.stderr
    lines = lines_by_place(result)
    assert lines["vault-db"].startswith("[NG]") and "control/planted" in lines["vault-db"] and "CANARY-7F3A" in lines["vault-db"]
    assert all(lines[place].startswith("[OK]") for place in PLACES if place != "vault-db")  # 置いた場所だけが NG
    assert "終了コード 1" in result.stdout


def test_a_canary_in_the_default_database_the_logs_the_memory_or_the_spans_makes_the_scan_fail(firestore_emulator_host):
    result = run_scan(firestore_emulator_host, "--plant", "default-db", "--plant", "log", "--plant", "memory", "--plant", "span")

    assert result.returncode == 1, result.stdout + result.stderr
    lines = lines_by_place(result)
    assert lines["vault-db"].startswith("[OK]")
    for place in ("(default)", "ログ", "メモリ(面談の状態)", "スパン"):
        assert lines[place].startswith("[NG]") and "CANARY-7F3A" in lines[place], lines[place]
    assert "残っている" in lines["メモリ(面談の状態)"]  # 送信のあとにも面談の状態があることも、見つける


def test_the_real_spans_of_the_interview_carry_the_text_when_the_capture_is_switched_on(firestore_emulator_host):
    # 対照: ADK は、既定でスパンに LLM の入力の全文を載せる。本番の設定(false)を外すと、置いたカナリアがなくても、スパンの検査が NG になる
    result = run_scan(firestore_emulator_host, ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS="true")

    assert result.returncode == 1, result.stdout + result.stderr
    lines = lines_by_place(result)
    assert lines["スパン"].startswith("[NG]") and "call_llm" in lines["スパン"] and "CANARY-7F3A" in lines["スパン"]
    assert all(lines[place].startswith("[OK]") for place in PLACES if place != "スパン")


def test_a_nonexistent_plant_place_is_refused(firestore_emulator_host):
    result = run_scan(firestore_emulator_host, "--plant", "nowhere")

    assert result.returncode == 2 and "invalid choice" in result.stderr  # argparse の拒否(カナリア検査は走らない)


# ----------------------------------------------------------------------
# 探す部品(プロセスの中)
# ----------------------------------------------------------------------


def test_needles_cover_the_canary_and_the_raw_numbers_but_not_the_rounded_ones():
    assert scan_script.needles_in("年収 650 万・下限 400 万") == []  # 丸めたあとの値は、残ってよい
    assert scan_script.needles_in("x CANARY-7F3A-REASON y 623.45") == ["CANARY-7F3A", "623.45"]
    assert scan_script.needles_in("神奈川県 7.3141") == ["7.3141", "神奈川"]


def test_documents_are_scanned_with_their_paths_and_sub_collections_and_bytes_are_left_out(firestore_client):
    firestore_client.collection("principals").document("p1").set({"note": "ok", "sealed": b"CANARY-7F3A-IN-BYTES"})
    firestore_client.collection("principals").document("p1").collection("ledger").document("row").set({"note": "年収 623.45 万"})
    firestore_client.collection("canary-CANARY-7F3A-path").document("d").set({"note": "ok"})

    place = scan_script.scan_documents("vault-db", firestore_client)

    assert place.scanned == 3 and place.unit == "文書"
    assert sorted(place.findings) == [
        "canary-CANARY-7F3A-path/d に CANARY-7F3A",  # パスの中のカナリアも見つける
        "principals/p1/ledger/row に 623.45",  # サブコレクションの下も探す
    ]  # bytes の項目の中の文字列は、探さない(封印した項目)
    assert "bytes の項目 1 件は除く" in place.note and not place.clean


def test_a_place_with_nothing_scanned_does_not_pass_and_the_lines_and_spans_are_reported_by_position():
    assert not scan_script.Place("ログ", 0, "行", []).clean
    assert "0 行だった" in scan_script.Place("ログ", 0, "行", []).render()
    place = scan_script.scan_lines("ログ", ["INFO a", "WARNING b CANARY-7F3A", "INFO c"])
    assert place.scanned == 3 and place.findings == ["2 行目に CANARY-7F3A"] and place.render().startswith("[NG] ログ: 3 行のうち 1 件")


def test_the_memory_check_flags_a_state_that_survives_the_submit_and_an_original_text_before_it():
    class Store:
        def __init__(self, states):
            self._states = states

        def __len__(self):
            return len(self._states)

    clean = scan_script.scan_memory("InterviewState(bands=..., revision=3)", Store({}))
    leftover = scan_script.scan_memory("InterviewState(text='CANARY-7F3A-SALARY')", Store({"p": "state 617.77"}))
    never_started = scan_script.scan_memory(None, Store({}))

    assert clean.clean
    assert leftover.findings == [
        "送信の前の状態に CANARY-7F3A",  # 原文は、送信の前でも、メモリに持たない
        "送信のあとのメモリに 617.77",
        "送信のあとも面談の状態が 1 件残っている",
    ]
    assert not never_started.clean  # 送信の前の状態がなかった: 調べた量が 0 なので、通らない


@pytest.mark.parametrize("missing_llm_input", [True, False])
def test_the_report_fails_when_the_premise_of_the_scan_broke(missing_llm_input):
    # 検査の前提(カナリアが LLM に届いたこと・金庫にポリシーが書かれたこと)が崩れたら、場所が全部きれいでも通らない
    place = scan_script.Place("ログ", 5, "行", [])
    report = scan_script.Report([place], ["原文が LLM に届いていない"] if missing_llm_input else [])

    assert report.passed is (not missing_llm_input)
    assert ("検査の前提" in report.render()) is missing_llm_input
