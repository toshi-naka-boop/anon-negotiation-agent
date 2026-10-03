"""ケース別のデモを、金庫・agents・レフェリーをつないで通し、DV-15 の判定を行うスクリプト(design.md §12.2 の DV-15。実装計画 ②・③-0)。

    uv run python scripts/run_demo.py --case 1 --live                       # 本物の ADK と Gemini(Vertex AI)で 1 回
    uv run python scripts/run_demo.py --case 1 --live --runs 2 --judge      # 2 回続けて DV-15 の基準で判定する(③-0 の完了条件)
    uv run python scripts/run_demo.py --case 1 [--scripted] [--runs N --judge]  # Gemini を呼ばず、台本のエージェントで(既定。このスクリプト自体の確かめ)
    uv run python scripts/run_demo.py --case 1 --record fixtures/replays/case1.jsonl   # 実行中のイベントを、リプレイの記録(JSONL)に書く
    uv run python scripts/run_demo.py --replay fixtures/replays/case1.jsonl [--speed 4]  # 記録を、記録どおりの間隔で流す(AC-09〜11)
    uv run python scripts/run_demo.py --case 1 --replay                    # 同じ。PATH を省くと、そのケースの fixtures/replays/case1.jsonl

ケース(§8.4。fixtures/case{N}.toml): 1=年収だけでは合意できないが、他の軸を動かせば合意できる / 2=両者が受けられる組み合わせがない
(双方に「なし」だけが返る) / 3=攻撃(mode=attack。求人側は攻撃者のエージェントを呼ぶ。--scripted の攻撃者は、探索線の上で年収を二分探索する)。

流れ(1 回の実行)
1. Firestore エミュレータを起動する(tests/conftest.py と同じく、gcloud を使わず、JDK 21 で jar を直接起動する)。--runs N でも、
   起動は 1 回(実行ごとに、別のプロジェクト ID のデータベースを使う)。
2. ケースのフィクスチャ(fixtures/case{N}.toml)を、金庫(vault-db)のテンプレートに置く。
3. 金庫の app を、同じプロセスの中で ASGI のまま(httpx の ASGITransport)つなぐ。サービス間の認証(ID トークン)は使わない。
   --live は、agents の app も ASGI でつなぎ、設定ファイルのモデル(gemini-3.5-flash)で、本物の ADK が Gemini を呼ぶ。
   --scripted(既定)は、agents を通さず、台本のエージェント(tests/scripted_negotiators.py。計画・決定の形)をレフェリーの send_turn の
   差し込み口に直接入れる(agents の ADK・A2A の経路は、--live と agents のテストで確かめる)。
4. web のレフェリーで、デモの交渉(ケース 3 は mode=attack、ほかは mode=demo)を 1 件作り、判定(judged)まで動かす。架空人物の途中確認は、
   フィクスチャの生の条件で答える(web.fictional_answerer.FixtureAnswerer)。レフェリーは、本番と同じく、送る前に物理の呼び出し数を
   `(default)`(エミュレータ)のカウンタで数える(web.llm_budget)。
5. 結果と診断を表示し、実行の記録を tmp/demo_runs/ に JSONL で残す。--judge なら、DV-15 の判定(下)も行う。エミュレータを止める。
   表示にも記録にも、交渉 ID などの ID と、プロジェクト ID は出さない。
   エージェントへの送信が終わるたびに、標準エラーへ進み具合を出す(本物の Gemini は、全体で数分かかり得る)。
   Ctrl-C・SIGTERM のときは、エミュレータを止めて、すぐ終わる。

リプレイ(§8.4・AC-21。記録の形と検査は scripts/replay_check.py)
- --record PATH: 実行中に、金庫のイベント列(候補者側・求人側それぞれの見え方)を、レフェリーが金庫を書き換えるたびに読み、読んだ時刻
  (observed_at。UNIX 秒)をつけて JSONL に書く。時刻は記録する側(このスクリプト)が付ける(金庫のモデルは変えない)。ヘッダの source は
  live か scripted(--live でなければ scripted)。--runs 1 のときだけ使える。検査(replay_check)に通らない記録(判定に届かなかった実行など)は
  書かず、終了コードを 1 にする。
- --replay [PATH]: 記録を検査してから、記録どおりの間隔で(--speed の倍率。既定 1.0)イベントを順に流す。最初に「リプレイ」であることを出す。
  PATH を省くと --case のケースの fixtures/replays/case{N}.jsonl。PATH と --case を両方渡したら、記録のケースが --case と同じことを確かめる。
  金庫・agents・エミュレータは使わず、ネットワークにも出ない。本物の Gemini の記録は、--live --record で取り直す。

エージェントへの送信の記録: レフェリーの send_turn の差し込み口を包み、物理の送信 1 回ごとに、役割・呼び出しの種類(計画・決定)・所要時間・結果・
`usage`(エージェントから受け取る。トークン数)を残す。物理の送信の数は、再試行(429・5xx の分を含む)も 1 回と数える。200 応答の数は、
`usage` が返ったものの数(出力が切れて印のついた応答を含む)。レフェリーが数えた物理の数(カウンタの値)も、別に記録する(送信の数と一致する。DV-17)。

DV-15 の判定(--judge。基準は config/params.toml の [agents.cost_targets]。スクリプトが計算し、手計算にしない。台帳 X-54・C-44・L12-5)
- 合意に届く(agreed)。
- 200 応答の呼び出しが、手の数(無効手と有効な途中確認を含む)× max_calls_per_move 以下、かつ max_successful_calls 以下。物理の送信の数は
  合否に入れない(429 が出る分を含むため。台帳 C-50)。クライアント側の自動再試行がないことは、DV-17 の偽の HTTP 層で確かめる。
- 費用が max_cost_usd_per_negotiation 以下。費用は、`usage` のトークン数に単価(版付き)を掛けて計算する。入力は、キャッシュ済みを含む
  prompt_tokens から、キャッシュ済みを引いた分に入力単価、キャッシュ済みにキャッシュ単価、出力と思考に出力単価。
- 1 呼び出しあたりの思考トークン数の平均が、thinking_tokens_baseline × max_avg_thinking_ratio 以下。
- `usage` が取れなかった呼び出し(応答は来たが、使用量を確かめられなかった失敗)が 1 つもない。
- 出力が切れた呼び出し(output_truncated)が 1 つもない(台帳 C-53)。
記録(JSONL)に、モデル ID・設定(思考の量・temperature・max_output_tokens・キャッシュの有無)のハッシュ・単価の版・呼び出しごとのトークン数・
物理の数・判定の結果を、機械可読で残す。

終了コード: 既定は、ケースの意図どおりに終われば 0、そうでなければ 1(ケース 1=合意(agreed) / ケース 2=合意に届かず「なし」で終わる /
ケース 3=判定(judged)まで届く)。--judge のときは、すべての実行が DV-15 の基準に合格すれば 0、1 つでも不合格なら 1(「合意に届く」の項目は、
ケース 1 以外では「ケースの意図どおりに終わる」になる)。
判定(judged)に届かないまま時間の上限(RUN_TIMEOUT_SECONDS)を過ぎたときも不合格(その時点までの状態を、診断として表示・記録する)。

--live には、ADK と google-genai の標準の環境変数が要る。プロジェクト ID は、コードにも設定ファイルにも書かない。
    GOOGLE_GENAI_USE_VERTEXAI=TRUE
    GOOGLE_CLOUD_PROJECT=<プロジェクト ID>
    GOOGLE_CLOUD_LOCATION=global
足りなければ、何も呼ばずに(エミュレータも起動せずに)説明を出して止まる。本物の GCP に接続するのは、--live の agents が
Gemini を呼ぶときだけ。Firestore は、エミュレータだけにつなぐ(demo- で始まるプロジェクト ID を使う)。--scripted は、
ネットワークに出ない(エミュレータは 127.0.0.1)。
"""

import argparse
import asyncio
import collections
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
import traceback
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 先頭に src(プロジェクトのコード)を足す。tests/ は --scripted の台本のエージェントを読むときだけ足す(scripted_sender)。
# scripts/ は、記録の形と検査(replay_check)を読むために足す(スクリプトとして動かすときは、すでに入っている)。
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from google.cloud import firestore  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

import replay_check  # noqa: E402
from agents.config import DEFAULT_AGENTS_CONFIG  # noqa: E402
from negotiation_core import Package, Side, TurnInput, Usage  # noqa: E402
from negotiation_core.log_privacy import mask_ids_in_logs  # noqa: E402
from vault.api_models import (  # noqa: E402
    CandidateParticipantRequest,
    CreateNegotiationRequest,
    EmployerParticipantRequest,
    EventViewItem,
)
from vault.app import create_app as create_vault_app  # noqa: E402
from vault.clock import SystemClock  # noqa: E402
from vault.config import DEFAULT_VAULT_CONFIG  # noqa: E402
from vault.firestore_client import VAULT_DATABASE  # noqa: E402
from vault.fixtures import CaseFixture, load_case_fixture, put_fixture_templates  # noqa: E402
from vault.models import NegotiationDocument, NegotiationResult, SideCounters  # noqa: E402
from vault.serialization import model_from_firestore  # noqa: E402
from vault.store import VaultStore  # noqa: E402
from web.config import DEFAULT_WEB_CONFIG  # noqa: E402
from web.fictional_answerer import FixtureAnswerer  # noqa: E402
from web.llm_budget import LlmBudget  # noqa: E402
from web.referee import AgentRole, NegotiationContext, Referee, RefereeDeps, SendTurn, is_truncated_output  # noqa: E402
from web.stages import StageStore  # noqa: E402
from web.vault_client import VaultClient  # noqa: E402

logger = logging.getLogger("run_demo")

Mode = Literal["live", "scripted"]
# 交渉 ID から、その側の見え方のイベントを金庫から読む口(--scripted の攻撃者が、金庫の答えを見るために使う)。
ReadEvents = Callable[[str], Awaitable[Sequence[EventViewItem]]]

# 実行の記録(JSONL)を置く場所。tmp/ は .gitignore に入っている。
RUN_RECORD_DIRECTORY = PROJECT_ROOT / "tmp" / "demo_runs"
# 交渉 1 件を、判定(judged)まで動かす時間の上限(秒)。見回りは動かさないので、止まったままの交渉をここで打ち切る。
# 上限を過ぎたら、次の LLM の呼び出しを始めずに止める(進行中の呼び出しは、終わるまで待つ。1 回は最長 60 秒)。
# 上限+60 秒が、Bash の 10 分の上限に収まるようにしてある。
RUN_TIMEOUT_SECONDS = 480.0

# 設定ファイル(判定の基準・単価・設定のハッシュの元)。
CONFIG_PATH = PROJECT_ROOT / "config" / "params.toml"

# Firestore エミュレータの起動(tests/conftest.py と同じ値)。
_JAVA_BIN = "/opt/homebrew/opt/openjdk@21/bin/java"
_EMULATOR_JAR = "/opt/homebrew/share/google-cloud-sdk/platform/cloud-firestore-emulator/cloud-firestore-emulator.jar"
_READY_MARKER = "Dev App Server is now running"
_STARTUP_TIMEOUT_SECONDS = 30
DEFAULT_DATABASE = "(default)"  # web 用(design.md §1.1)

# agents の URL。A2A のクライアントが組み立てる URL の元になるだけで、通信はすべて agents の app につながる。
_AGENTS_BASE_URL = "http://agents.test"

# --live に要る環境変数(ADK と google-genai の標準のもの)と、説明に出す値の例。
_LIVE_ENVIRONMENT = (
    ("GOOGLE_GENAI_USE_VERTEXAI", "TRUE"),
    ("GOOGLE_CLOUD_PROJECT", "<PROJECT_ID>"),
    ("GOOGLE_CLOUD_LOCATION", "global"),
)

# 攻撃のケース(§8.4: ケース 3)。交渉は mode=attack で作り、レフェリーは求人側に攻撃者のエージェントを呼ぶ(§4.1)。
ATTACK_CASES = frozenset({3})
# --live の攻撃で、攻撃者の求人エージェントへ毎手番渡す指示(400 文字以内。§8.2)。審査員が自然文で出す指示の代わりの、実演用の固定の文
# (暫定)。探索線(fixtures/case3.toml のコメント)の上で、年収を 600 万から 50 万ずつ上げて、候補者が受ける最低の年収を探る。
ATTACKER_INSTRUCTION = (
    "候補者が受ける最低の年収を探ってください。年収以外の条件は固定し、年収だけを変えた提案を、毎手番 1 つ出してください。"
    "固定する条件は、リモート週 1 日・当直なし・昇給見直し 6 か月・研修あり・副業可・入職 3 か月以内です。"
    "最初は年収 600 万、断られるたびに 50 万ずつ上げてください。受け入れられたら、それで終わりです。"
)

# --scripted の最初の手(7 巡目のシミュレーション・tests/test_fixtures.py の 36 通りの始め方の 1 つ)。
# 候補者はケース 1〜3、求人はケース 1・2 の最初の提案(どれも、そのケースの本人が受けられる組み合わせ)。
# ケース 3 の求人は攻撃者で、最初の手を持たない(探索線の上で、年収を二分探索する。tests/scripted_negotiators.py)。
_SCRIPTED_CASES = (1, 2, 3)
_SCRIPTED_CANDIDATE_OPENING = dict(salary=900, remote_days=3, night_duty=0, review_months=6)
_SCRIPTED_EMPLOYER_OPENING = dict(salary=500, remote_days=0, night_duty=4, review_months=12)
_SCRIPTED_CATEGORICAL = dict(training="available", side_job="allowed", start="within_3_months")

# --scripted の使用量。台本は LLM を呼ばないので、判定の仕組み(費用の計算・思考の平均など)を動かすための、固定の合成値
# (実際のトークン数ではない。モデル ID は scripted)。
SCRIPTED_USAGE = Usage(
    model="scripted", prompt_tokens=2000, cached_tokens=0, thoughts_tokens=200, output_tokens=60, requests=1
)

_LIKELIHOOD_LABELS = {"high": "高", "medium": "中", "none": "なし"}
_SIDE_LABELS: dict[Side, str] = {"candidate": "候補者側", "employer": "求人側"}
_ID_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")  # 交渉 ID・依頼者 ID(16 桁の 16 進数。§2.7)


# ----------------------------------------------------------------------
# Firestore エミュレータ
# ----------------------------------------------------------------------


# 起動中のエミュレータ。Ctrl-C・SIGTERM のときに、すぐ止めて終わるため(_exit_at_signal)。
_running_emulators: list[subprocess.Popen] = []


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop_emulator(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@contextlib.contextmanager
def firestore_emulator() -> Iterator[str]:
    """Firestore エミュレータを起動して host を渡し、終わったら止める(tests/conftest.py と同じ起動)。

    本物の GCP には接続しない: 呼び出し側が Firestore のクライアントを作るより前に、FIRESTORE_EMULATOR_HOST をここで
    エミュレータへ向ける(終わったら元に戻す)。
    """
    for path in (_JAVA_BIN, _EMULATOR_JAR):
        if not Path(path).exists():
            raise RuntimeError(f"Firestore エミュレータの起動に要るファイルがありません: {path}")
    port = _find_free_port()
    host = f"127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            _JAVA_BIN,
            "-Duser.language=en",
            "-cp",
            _EMULATOR_JAR,
            "com.google.cloud.datastore.emulator.firestore.CloudFirestore",
            "start",
            "--host=127.0.0.1",
            f"--port={port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _running_emulators.append(process)
    tail: collections.deque[str] = collections.deque(maxlen=50)  # 起動に失敗したときの説明用に、末尾だけ持つ
    ready = threading.Event()

    def drain() -> None:
        # 出力を読み続ける(読まないと、パイプがいっぱいになってエミュレータが止まる)。
        for line in iter(process.stdout.readline, ""):
            tail.append(line)
            if _READY_MARKER in line:
                ready.set()

    threading.Thread(target=drain, daemon=True).start()
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while not ready.wait(0.5):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Firestore エミュレータが時間内に起動しなかった:\n" + "".join(tail))
        previous = os.environ.get("FIRESTORE_EMULATOR_HOST")
        os.environ["FIRESTORE_EMULATOR_HOST"] = host
        try:
            yield host
        finally:
            if previous is None:
                os.environ.pop("FIRESTORE_EMULATOR_HOST", None)
            else:
                os.environ["FIRESTORE_EMULATOR_HOST"] = previous
    finally:
        _stop_emulator(process)
        _running_emulators.remove(process)


# ----------------------------------------------------------------------
# 設定(判定の基準・単価・設定のハッシュ)
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CostTargets:
    """DV-15 の判定の基準と単価(config/params.toml の [agents.cost_targets])。基準の値の正本は設定ファイル。"""

    max_cost_usd_per_negotiation: float
    max_calls_per_move: int
    max_successful_calls: int
    thinking_tokens_baseline: int
    max_avg_thinking_ratio: float
    price_version: str
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float


def load_cost_targets(path: Path = CONFIG_PATH) -> CostTargets:
    """設定ファイルの [agents.cost_targets] を読む。足りない項目があれば、ValueError。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    section = raw.get("agents", {}).get("cost_targets")
    if section is None:
        raise ValueError(f"{path} is missing the [agents.cost_targets] section")
    try:
        return CostTargets(
            max_cost_usd_per_negotiation=float(section["max_cost_usd_per_negotiation"]),
            max_calls_per_move=int(section["max_calls_per_move"]),
            max_successful_calls=int(section["max_successful_calls"]),
            thinking_tokens_baseline=int(section["thinking_tokens_baseline"]),
            max_avg_thinking_ratio=float(section["max_avg_thinking_ratio"]),
            price_version=str(section["price_version"]),
            input_usd_per_million=float(section["input_usd_per_million"]),
            cached_input_usd_per_million=float(section["cached_input_usd_per_million"]),
            output_usd_per_million=float(section["output_usd_per_million"]),
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [agents.cost_targets] key: {exc}") from exc


def load_agents_settings(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """[agents] の、LLM の呼び出しの設定(思考の量・temperature・max_output_tokens)。ハッシュの元(キャッシュの有無は別)。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    section = raw.get("agents", {})
    return {
        key: section.get(key)
        for key in ("plan_thinking_level", "decide_thinking_level", "temperature", "max_output_tokens")
    }


def settings_hash(model_id: str, settings: Mapping[str, Any]) -> str:
    """モデル ID と、設定(思考の量・temperature・max_output_tokens・キャッシュの有無)のハッシュ(先頭 12 桁)。

    キャッシュの有無は、いまは常に「使わない」(spec〔29〕。明示のキャッシュを使うときは P-7 をユーザーに聞く。§4.2)。
    """
    material = {**settings, "model": model_id, "explicit_cache": False}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:12]


# ----------------------------------------------------------------------
# エージェントへの送信の記録
# ----------------------------------------------------------------------

SendKind = Literal["ok", "truncated", "unusable", "transient", "error"]


@dataclasses.dataclass(frozen=True)
class SendRecord:
    """エージェントへの物理の送信 1 回の記録(診断と DV-15 の判定の材料)。

    kind: ok=200 応答で `usage` つき / truncated=出力が max_output_tokens で切れた応答(`usage` は、あれば持つ) /
    unusable=一時的でない失敗(ValueError。応答の封筒の検証の失敗・出力が JSON でない・受信口の拒否など。使用量を確かめられない) /
    transient=一時的な失敗(通信・時間切れ。再試行される) / error=ほかの例外。error は、失敗の例外の型名だけ(メッセージは持たない)。
    """

    n: int
    role: str
    phase: str
    seconds: float
    kind: SendKind
    error: str | None
    usage: Usage | None
    output: str | None  # kind=ok のときの Plan・Move の JSON(JSONL にだけ書く)

    @property
    def has_usage(self) -> bool:
        return self.usage is not None

    @property
    def usage_missing(self) -> bool:
        """応答は来たが、使用量を確かめられなかった(DV-15 で、1 つでもあれば不合格)。"""
        return self.kind == "unusable" or (self.kind == "truncated" and self.usage is None)


class SendRecorder:
    """レフェリーの send_turn を包み、物理の送信 1 回ごとに SendRecord を残す(送信を止めたり、変えたりはしない)。

    レフェリーが再試行するたびに、このクラスが 1 回ずつ記録するので、記録の数が物理の送信の数になる。
    """

    def __init__(self, send_turn: SendTurn, on_send: Callable[[SendRecord], None] | None = None) -> None:
        self._send_turn = send_turn
        self._on_send = on_send  # 送信が終わるたびに呼ぶ(進み具合の表示用)
        self.records: list[SendRecord] = []

    async def __call__(self, role: AgentRole, turn_input: TurnInput, *, nid: str, timeout_s: float):
        started = time.perf_counter()
        try:
            payload, usage = await self._send_turn(role, turn_input, nid=nid, timeout_s=timeout_s)
        except (ConnectionError, TimeoutError, asyncio.CancelledError) as exc:
            self._record(role, turn_input, started, "transient", type(exc).__name__, None, None)
            raise
        except ValueError as exc:
            if is_truncated_output(exc):
                self._record(role, turn_input, started, "truncated", type(exc).__name__, getattr(exc, "usage", None), None)
            else:
                self._record(role, turn_input, started, "unusable", type(exc).__name__, None, None)
            raise
        except Exception as exc:
            self._record(role, turn_input, started, "error", type(exc).__name__, None, None)
            raise
        self._record(role, turn_input, started, "ok", None, usage, json.dumps(payload, ensure_ascii=False))
        return payload, usage

    def _record(self, role, turn_input, started, kind, error, usage, output) -> None:
        record = SendRecord(
            n=len(self.records) + 1,
            role=str(role),
            phase=turn_input.phase,
            seconds=time.perf_counter() - started,
            kind=kind,
            error=error,
            usage=usage,
            output=output,
        )
        self.records.append(record)
        if self._on_send is not None:
            self._on_send(record)


# ----------------------------------------------------------------------
# リプレイの記録(実行中のイベントの観測。§8.4)
# ----------------------------------------------------------------------


class EventObserver:
    """金庫のイベント列(側ごとの見え方)の新しい分を読み、読んだ時刻(observed_at。UNIX 秒)をつけて集める。

    時刻は、記録する側(このクラス)が付ける。金庫のモデルには時刻を足さない。observe を呼ぶたびに、各側の
    after_seq 以降だけを読む。時刻は、前に付けた時刻より戻らない(時計が戻っても、単調非減少にする)。
    """

    def __init__(self, vault: VaultClient, nid: str, clock: Callable[[], float] = time.time) -> None:
        self._vault = vault
        self._nid = nid
        self._clock = clock
        self._last_seq: dict[Side, int] = {"candidate": 0, "employer": 0}
        self._last_time = 0.0
        self.events: list[replay_check.ReplayEvent] = []

    async def observe(self, first: Side = "candidate") -> None:
        """新しいイベントを読む。同じ回に読んだ双方のイベントは同じ時刻で、first の側を先に並べる(書き込みをした側が先)。"""
        self._last_time = max(self._last_time, self._clock())
        for side in (first, "employer" if first == "candidate" else "candidate"):
            for item in await self._vault.get_events(self._nid, side, after_seq=self._last_seq[side]):
                self.events.append(replay_check.ReplayEvent(side, self._last_time, item))
                self._last_seq[side] = item.seq


class ObservingVault:
    """レフェリーに渡す金庫クライアントの包み。金庫を書き換える操作(手・途中確認の回答・費用の停止など)が成功した直後に、
    observer.observe を呼ぶ。読み出しは素通し。書き換えのたびに読むので、イベントは起きた順に並ぶ。"""

    _WRITES = frozenset({"post_move", "post_principal_answer", "stop_cost_limit", "control", "expire"})

    def __init__(self, inner: VaultClient, observer: EventObserver) -> None:
        self._inner = inner
        self._observer = observer

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if name not in self._WRITES:
            return attribute

        async def write_then_observe(*args, **kwargs):
            result = await attribute(*args, **kwargs)
            request = args[1] if len(args) > 1 else kwargs.get("request")  # 手と回答は side を持つ。書いた側を先に並べる
            await self._observer.observe(getattr(request, "side", "candidate"))
            return result

        return write_then_observe


class RunTimeout(BaseException):
    """時間の上限を過ぎた(run_demo が受け止める)。

    BaseException にして、レフェリーの「約束にない例外は、その手番を無効手にする」処理に飲み込まれないようにする。
    asyncio の cancel で止めないのは、金庫・agents を同じプロセスの ASGI でつなぐと、進行中の A2A の呼び出しが cancel を
    受け付けないため(呼び出しの中の LLM が終わるまで、最長 45 秒、cancel が握りつぶされる。実際の HTTP では、すぐ効く)。
    """


def with_deadline(send_turn: SendTurn, deadline: float) -> SendTurn:
    """send_turn を、deadline(time.monotonic の時刻)を過ぎたら、呼ばずに RunTimeout を投げるものにする。"""

    async def send(role: AgentRole, turn_input: TurnInput, *, nid: str, timeout_s: float):
        if time.monotonic() >= deadline:
            raise RunTimeout
        return await send_turn(role, turn_input, nid=nid, timeout_s=timeout_s)

    return send


def scripted_sender(case: int, read_candidate_events: ReadEvents | None = None) -> SendTurn:
    """--scripted の、Gemini の代わりのエージェント(ネットワークに出ない。agents を通さない)。

    tests/scripted_negotiators.py の台本のエージェント(指示文どおりの探し方。両側とも hybrid)。Gemini と同じく TurnInput だけを見て、
    計画(phase=plan)なら Plan、決定(phase=decide)なら Move を返す。使用量は固定の合成値(SCRIPTED_USAGE)。

    攻撃のケース(ATTACK_CASES)は、求人側が攻撃者の台本(探索線の上で年収を二分探索する。最悪の場合の攻撃者として、候補者側の金庫の答えを
    見る)で、候補者は、受けられる提案が来ても受けずに対案を出す(accepts=False)ので、1 つの交渉で二分探索が最後まで進む。
    read_candidate_events(交渉 ID → 候補者側のイベント)がそのために要る。
    """
    if str(PROJECT_ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "tests"))
    from scripted_negotiators import Negotiator, ScriptedAttackNegotiators, ScriptedAttacker, ScriptedNegotiators, Strategy

    candidate_opening = Package(**_SCRIPTED_CANDIDATE_OPENING, **_SCRIPTED_CATEGORICAL)
    if case in ATTACK_CASES:
        if read_candidate_events is None:
            raise ValueError("an attack case needs read_candidate_events")
        return ScriptedAttackNegotiators(
            Negotiator(Strategy("hybrid", accepts=False), candidate_opening),
            ScriptedAttacker(read_candidate_events),
            usage=SCRIPTED_USAGE,
        )
    return ScriptedNegotiators(
        Negotiator(Strategy("hybrid"), candidate_opening),
        Negotiator(Strategy("hybrid"), Package(**_SCRIPTED_EMPLOYER_OPENING, **_SCRIPTED_CATEGORICAL)),
        usage=SCRIPTED_USAGE,
    )


def build_agents_app() -> Starlette:
    """--live の agents の app を作る(設定ファイルのモデル名のまま。ADK が最初の呼び出しのときに、環境変数の Vertex AI につなぐ)。"""
    from agents.app import create_app as create_agents_app  # --live だけが使うので、ここで読む(--scripted は agents を使わない)

    return create_agents_app()


@contextlib.contextmanager
def agents_over_asgi(app: Starlette) -> Iterator[None]:
    """web(レフェリー)から agents への A2A の呼び出しを、ネットワークを通さず、agents の app に ASGI のままつなぐ。

    agents.client.send_turn が HTTP クライアントを作る口(_open_http_client)を差し替える(tests/test_agent_client.py と
    同じ口)。終わったら元に戻す。
    """
    import agents.client as agents_client  # --live だけが使う

    transport = httpx.ASGITransport(app=app)
    original = agents_client._open_http_client
    agents_client._open_http_client = lambda timeout_s: httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(timeout_s)
    )
    try:
        yield
    finally:
        agents_client._open_http_client = original


# ----------------------------------------------------------------------
# 交渉を 1 件動かして、診断の材料を集める
# ----------------------------------------------------------------------


@dataclasses.dataclass
class DemoRun:
    """1 回の実行の結果と、診断・判定の材料。nid は金庫から読むためだけに持ち、表示にも記録にも出さない。"""

    case: int
    mode: Mode
    model: str  # 表示用の説明
    model_id: str  # 記録・ハッシュ用のモデル ID(live は設定ファイルのモデル名、scripted は "scripted")
    location: str
    started_at: dt.datetime
    nid: str
    status: str
    end_reason: str | None  # 金庫の中の終了理由(診断用。外には出ない値)
    result: NegotiationResult | None
    events: dict[Side, list[EventViewItem]]  # 側ごとの見え方
    counters: dict[Side, SideCounters]
    calls: list[SendRecord]  # エージェントへの物理の送信(再試行を含む。送った順)
    referee_counted_calls: int | None  # レフェリーが送る前に数えた物理の数(交渉ごとのカウンタ)。数えていなければ None
    elapsed_seconds: float
    timed_out: bool
    timeout_seconds: float
    # 実行中に観測したイベント(読んだ時刻つき。観測した順)。--record のときだけ集める(リプレイの記録。§8.4)
    observed: list[replay_check.ReplayEvent] = dataclasses.field(default_factory=list)

    def invalid_moves_by_reason(self) -> collections.Counter:
        """無効手の数(理由ごと。両側の合計)。"""
        return collections.Counter(
            item.reason for items in self.events.values() for item in items if item.kind == "invalid"
        )

    @property
    def moves_made(self) -> int:
        """手の数(無効手と有効な途中確認を含む)。金庫の側ごとの手数(有効な確かめと有効な途中確認を除く)＋ 有効な途中確認。"""
        return sum(c.moves_used + c.principal_checks_used for c in self.counters.values())

    @property
    def successful_calls(self) -> int:
        """200 応答の数(`usage` が返ったもの)。"""
        return sum(1 for call in self.calls if call.has_usage)


def describe_backend(mode: Mode, environ: Mapping[str, str]) -> tuple[str, str]:
    """使うモデルの名前と、場所(最初に表示する)。プロジェクト ID は、書かない。"""
    if mode == "scripted":
        return "なし(台本のエージェント。Gemini は呼ばない)", "なし(ネットワークに出ない)"
    location = environ.get("GOOGLE_CLOUD_LOCATION", "").strip() or "(環境変数 GOOGLE_CLOUD_LOCATION が未設定)"
    return DEFAULT_AGENTS_CONFIG.model, location


def check_live_environment(environ: Mapping[str, str], case: int) -> str | None:
    """--live に要る環境変数がそろっているか。そろっていなければ、止めるときに出す説明を返す(そろっていれば None)。

    値(プロジェクト ID など)は、説明に書かない。
    """
    problems = []
    missing = [name for name, _ in _LIVE_ENVIRONMENT if not environ.get(name, "").strip()]
    if missing:
        problems.append("足りない環境変数: " + "、".join(missing))
    vertex = environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip()
    if vertex and vertex.lower() not in ("true", "1"):
        problems.append("GOOGLE_GENAI_USE_VERTEXAI は TRUE にしてください")
    if not problems:
        return None
    example = " ".join(f"{name}={value}" for name, value in _LIVE_ENVIRONMENT)
    return "\n".join(
        [
            "--live を始められません。" + "。".join(problems) + "。",
            "必要な環境変数は、ADK と google-genai の標準のものです(プロジェクト ID は、コードにも設定ファイルにも書きません)。",
            f"例: {example} uv run python scripts/run_demo.py --case {case} --live",
            "何も呼ばずに止めました(Firestore エミュレータも起動していません)。",
        ]
    )


async def run_demo(
    *,
    fixture: CaseFixture,
    mode: Mode,
    vault_db: firestore.Client,
    default_db: firestore.Client,
    timeout_seconds: float = RUN_TIMEOUT_SECONDS,
    on_send: Callable[[SendRecord], None] | None = None,
    record_events: bool = False,
) -> DemoRun:
    """デモの交渉を 1 件作り、レフェリーで判定(judged)まで動かす。vault_db・default_db は、エミュレータの vault-db・(default)。

    金庫の app は、同じプロセスの中で ASGI のままつなぐ(サービス間の認証は使わない)。--live は、agents の app も同じようにつなぐ。
    判定まで届かなかったとき(時間切れ)も、その時点の状態を診断の材料として返す。
    ケース 3(ATTACK_CASES)の交渉は mode=attack で作り、求人側のエージェントは攻撃者になる。
    record_events=True なら、レフェリーが金庫を書き換えるたびにイベントを読み、読んだ時刻つきで DemoRun.observed に残す(--record)。
    """
    started_at = dt.datetime.now(dt.timezone.utc)
    negotiation_mode = "attack" if fixture.case in ATTACK_CASES else "demo"
    put_fixture_templates(vault_db, fixture)
    clock = SystemClock()
    store = VaultStore(db=vault_db, clock=clock, config=DEFAULT_VAULT_CONFIG)
    vault_transport = httpx.ASGITransport(app=create_vault_app(store))
    async with httpx.AsyncClient(transport=vault_transport, base_url="http://vault") as vault_http:
        vault = VaultClient(vault_http)
        created = await vault.create_negotiation(
            CreateNegotiationRequest(
                request_id=uuid.uuid4().hex,  # 毎回新しい乱数(同じ request_id は、すでに作った交渉を返すため)
                mode=negotiation_mode,
                candidate=CandidateParticipantRequest(is_fictional=True, template_id=fixture.candidate.template_id),
                employer=EmployerParticipantRequest(template_id=fixture.employer.template_id),
            )
        )
        if created.status != "created" or created.nid is None:
            raise RuntimeError(f"交渉を作れなかった(理由: {created.reason})")
        nid = created.nid
        # 交渉の作成直後に、web が段の状態(交渉ごとの物理の数を持つ)を作るのと同じ(web.api の register_created_negotiation)。
        await StageStore(default_db, clock, DEFAULT_WEB_CONFIG.retention).ensure(nid, None)
        budget = LlmBudget(default_db, clock, DEFAULT_WEB_CONFIG.llm_budget)

        if mode == "live":
            from web.app import bind_agents_client  # --live だけが使う(agents を束ねる)

            inner: SendTurn = bind_agents_client(_AGENTS_BASE_URL)
            agents_context = agents_over_asgi(build_agents_app())
        else:
            inner = scripted_sender(fixture.case, lambda attack_nid: vault.get_events(attack_nid, "candidate"))
            agents_context = contextlib.nullcontext()
        recorder = SendRecorder(inner, on_send)
        observer = EventObserver(vault, nid) if record_events else None
        deps = RefereeDeps(
            vault=ObservingVault(vault, observer) if observer is not None else vault,
            send_turn=with_deadline(recorder, time.monotonic() + timeout_seconds),
            answerer=FixtureAnswerer(fixture),
            attacker_instruction=(lambda _nid: ATTACKER_INSTRUCTION) if negotiation_mode == "attack" else None,
            llm_budget=budget,
        )
        referee = Referee(NegotiationContext(nid=nid, mode=negotiation_mode, candidate_principal_id=None), deps)
        started = time.perf_counter()
        timed_out = False
        with agents_context:
            try:
                await referee.run()
            except RunTimeout:
                timed_out = True
        elapsed = time.perf_counter() - started
        if observer is not None:
            await observer.observe()  # 書き換えの直後の読みで、取りこぼしたものがないように、最後にもう一度読む
        events = {side: await vault.get_events(nid, side) for side in _SIDE_LABELS}
        referee_counted = await budget.negotiation_count(nid)

    # 診断のため、金庫の文書を直接読む(終了理由は、金庫の中だけの値で、API には出ない)。
    document = model_from_firestore(NegotiationDocument, store._negotiation_ref(nid).get().to_dict())
    model, location = describe_backend(mode, os.environ)
    return DemoRun(
        case=fixture.case,
        mode=mode,
        model=model,
        model_id=DEFAULT_AGENTS_CONFIG.model if mode == "live" else "scripted",
        location=location,
        started_at=started_at,
        nid=nid,
        status=document.status,
        end_reason=document.end_reason,
        result=document.result,
        events=events,
        counters={"candidate": document.counters.candidate, "employer": document.counters.employer},
        calls=recorder.records,
        referee_counted_calls=referee_counted,
        elapsed_seconds=elapsed,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        observed=observer.events if observer is not None else [],
    )


# ----------------------------------------------------------------------
# DV-15 の判定
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Check:
    """判定の 1 項目。detail は、実測の値と基準(ID を含まない)。"""

    name: str
    passed: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class Judgement:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def usage_cost_usd(usage: Usage, targets: CostTargets) -> float:
    """1 回の呼び出しの費用(ドル)。prompt_tokens は、キャッシュ済みを含む(Gemini の prompt_token_count と同じ)と読む。

    入力: (prompt_tokens − cached_tokens)に入力単価、cached_tokens にキャッシュ単価。出力: output_tokens ＋ thoughts_tokens(思考込み)に
    出力単価。単価は 100 万トークンあたり。
    """
    uncached = max(usage.prompt_tokens - usage.cached_tokens, 0)
    return (
        uncached * targets.input_usd_per_million
        + usage.cached_tokens * targets.cached_input_usd_per_million
        + (usage.output_tokens + usage.thoughts_tokens) * targets.output_usd_per_million
    ) / 1_000_000


def run_cost_usd(run: DemoRun, targets: CostTargets) -> float:
    """交渉 1 回の費用(ドル)。`usage` が返った送信の合計。"""
    return sum(usage_cost_usd(call.usage, targets) for call in run.calls if call.usage is not None)


# ケースごとの「意図どおりの終わり方」(§8.4): (判定の項目名, 意図どおりのときの表示, そうでないときの表示)。
_INTENDED_ENDS: dict[int, tuple[str, str, str]] = {
    1: ("agreed", "合意(agreed)", "合意に届かなかった"),
    2: ("as_intended", "合意に届かず「なし」で終わった(意図どおり)", "意図と違う終わり方(合意した、または判定に届かなかった)"),
    3: ("as_intended", "攻撃の交渉が判定(judged)まで届いた", "攻撃の交渉が判定(judged)に届かなかった"),
}


def ended_as_intended(run: DemoRun) -> bool:
    """実行が、ケースの意図どおりに終わったか(§8.4)。時間切れや、判定(judged)に届かなかった実行は、どのケースでも意図どおりではない。

    ケース 1=合意(agreed) / ケース 2=合意に届かず、結果が「なし」 / ケース 3(攻撃)=判定まで届いた(合意でも、攻撃者が終えてもよい)。
    """
    if run.timed_out or run.status != "judged":
        return False
    if run.case == 2:
        return run.end_reason != "agreed" and run.result is not None and run.result.likelihood == "none"
    if run.case in ATTACK_CASES:
        return True
    return run.end_reason == "agreed"


def judge(run: DemoRun, targets: CostTargets) -> Judgement:
    """DV-15 の判定(モジュールの docstring)。実測の値だけで決め、手で直さない。"""
    checks: list[Check] = []

    name = _INTENDED_ENDS.get(run.case, _INTENDED_ENDS[1])[0]
    checks.append(Check(name, ended_as_intended(run), f"金庫の終了理由: {run.end_reason or '(終了していない)'}"))

    successful, moves = run.successful_calls, run.moves_made
    limit = min(moves * targets.max_calls_per_move, targets.max_successful_calls)
    checks.append(
        Check(
            "successful_calls",
            successful <= limit,
            f"200 応答の呼び出し {successful} 回 / 上限 {limit} 回"
            f"(手の数 {moves} × {targets.max_calls_per_move} と {targets.max_successful_calls} の小さい方)。"
            f"物理の送信 {len(run.calls)} 回は、合否に入れない",
        )
    )

    cost = run_cost_usd(run, targets)
    checks.append(
        Check(
            "cost",
            cost <= targets.max_cost_usd_per_negotiation,
            f"費用 ${cost:.4f} / 上限 ${targets.max_cost_usd_per_negotiation:.4f}(単価の版 {targets.price_version})",
        )
    )

    with_usage = [call for call in run.calls if call.usage is not None]
    average_thinking = sum(call.usage.thoughts_tokens for call in with_usage) / len(with_usage) if with_usage else None
    thinking_limit = targets.thinking_tokens_baseline * targets.max_avg_thinking_ratio
    checks.append(
        Check(
            "thinking",
            average_thinking is not None and average_thinking <= thinking_limit,
            "思考トークンの平均 "
            + ("(使用量なし)" if average_thinking is None else f"{average_thinking:.1f}")
            + f" / 上限 {thinking_limit:.1f}(基準値 {targets.thinking_tokens_baseline} × {targets.max_avg_thinking_ratio})",
        )
    )

    missing = sum(1 for call in run.calls if call.usage_missing)
    checks.append(Check("usage", missing == 0, f"`usage` が取れなかった呼び出し {missing} 回"))

    truncated = run.invalid_moves_by_reason().get("output_truncated", 0)
    truncated_sends = sum(1 for call in run.calls if call.kind == "truncated")
    checks.append(
        Check(
            "no_truncation",
            truncated == 0 and truncated_sends == 0,
            f"出力が切れた呼び出し {max(truncated, truncated_sends)} 回(無効手 output_truncated {truncated} 件)",
        )
    )
    return Judgement(tuple(checks))


# ----------------------------------------------------------------------
# 表示と記録
# ----------------------------------------------------------------------


def format_package(package: Package | None) -> str:
    if package is None:
        return "なし"
    return (
        f"年収 {package.salary} / リモート {package.remote_days} / 当直 {package.night_duty} / "
        f"見直し {package.review_months} / 研修 {package.training} / 副業 {package.side_job} / 入職 {package.start}"
    )


def _send_outcome(call: SendRecord) -> str:
    return "ok" if call.kind == "ok" else f"{call.kind} {call.error}"


def _send_tokens(call: SendRecord) -> str:
    if call.usage is None:
        return ""
    usage = call.usage
    return (
        f"  トークン 入力 {usage.prompt_tokens}(うちキャッシュ {usage.cached_tokens}) / 出力 {usage.output_tokens}"
        f" / 思考 {usage.thoughts_tokens}"
    )


def _print_progress(call: SendRecord) -> None:
    """エージェントへの送信が終わるたびの進み具合(標準エラー。本物の Gemini は、全体で数分かかり得る)。"""
    print(
        f"  送信 #{call.n} {call.role} {call.phase} {call.seconds:.2f} s {_send_outcome(call)}",
        file=sys.stderr,
        flush=True,
    )


def _format_event(item: EventViewItem) -> str:
    parts = [f"{item.seq:>2} {item.kind:<16}"]
    if item.package is not None:
        parts.append(format_package(item.package))
    if item.own_evaluation is not None:
        parts.append(f"→ {item.own_evaluation}")
    if item.reason is not None:
        parts.append(f"理由 {item.reason}")
    if item.attempted_move is not None:
        parts.append(f"(打とうとした手 {item.attempted_move})")
    if item.answer is not None:
        parts.append(f"回答 {item.answer}")
    if item.result is not None:
        parts.append(f"最終結果 {_LIKELIHOOD_LABELS[item.result.likelihood]} {format_package(item.result.package)}")
    return " ".join(parts)


def format_report(run: DemoRun, targets: CostTargets | None = None) -> str:
    """結果と診断の表示(ID は出さない)。targets を渡すと、費用も出す。"""
    limits = DEFAULT_VAULT_CONFIG.limits
    lines: list[str] = ["", "=== 結果 ==="]
    if run.timed_out:
        lines.append(f"時間切れ: {run.timeout_seconds:.0f} 秒以内に判定(judged)に届かなかった(金庫の状態: {run.status})")
    if run.result is None:
        lines.append("見込み・組み合わせ: なし(判定に届いていない)")
    else:
        lines.append(f"見込み: {_LIKELIHOOD_LABELS[run.result.likelihood]}({run.result.likelihood})")
        lines.append(f"組み合わせ: {format_package(run.result.package)}")
    lines.append(f"金庫の中の終了理由(診断用): {run.end_reason or '(終了していない)'}")
    lines.append(f"交渉の所要時間: {run.elapsed_seconds:.1f} s")

    lines += ["", "=== 双方の手の並び(側ごとの見え方) ==="]
    for side, label in _SIDE_LABELS.items():
        lines.append(f"[{label}]")
        lines += [f"  {_format_event(item)}" for item in run.events[side]] or ["  (なし)"]

    calls = run.calls
    by_role = collections.Counter(call.role for call in calls)
    by_phase = collections.Counter(call.phase for call in calls)
    counted = "数えていない" if run.referee_counted_calls is None else f"{run.referee_counted_calls} 回"
    employer_sends = by_role["employer"] + by_role["attacker"]  # 攻撃の交渉の求人側は、攻撃者のエージェント
    lines += ["", "=== エージェントへの送信 ==="]
    lines.append(
        f"物理の送信 計 {len(calls)} 回(候補者 {by_role['candidate']}・求人 {employer_sends}。"
        f"計画 {by_phase['plan']}・決定 {by_phase['decide']})。200 応答(`usage` つき) {run.successful_calls} 回・"
        f"レフェリーが数えた物理の数 {counted}"
    )
    for call in calls:
        lines.append(
            f"  #{call.n:<3} {call.role:<9} {call.phase:<6} {call.seconds:6.2f} s  {_send_outcome(call)}{_send_tokens(call)}"
        )
    if calls:
        seconds = [call.seconds for call in calls]
        lines.append(f"  合計 {sum(seconds):.1f} s / 平均 {sum(seconds) / len(seconds):.2f} s / 最長 {max(seconds):.2f} s")
    with_usage = [call.usage for call in calls if call.usage is not None]
    if with_usage:
        lines.append(
            f"  トークン合計 入力 {sum(u.prompt_tokens for u in with_usage)}(うちキャッシュ {sum(u.cached_tokens for u in with_usage)})"
            f" / 出力 {sum(u.output_tokens for u in with_usage)} / 思考 {sum(u.thoughts_tokens for u in with_usage)}"
        )
    if targets is not None:
        note = "(台本の合成値の使用量から計算。実際の費用ではない)" if run.mode == "scripted" else ""
        lines.append(f"  費用 ${run_cost_usd(run, targets):.4f}(単価の版 {targets.price_version}){note}")

    lines += ["", "=== 側ごとの手数と評価の使用量(金庫の回数。上限は config/params.toml) ==="]
    for side, label in _SIDE_LABELS.items():
        counters = run.counters[side]
        lines.append(
            f"  {label}: 手数 {counters.moves_used}/{limits.moves_budget_per_side}"
            f" / 評価 {counters.evaluations_used}/{limits.evaluation_budget_per_side}"
            f" / 途中確認 {counters.principal_checks_used}/{limits.principal_checks_per_side}"
        )

    invalid = run.invalid_moves_by_reason()
    lines += ["", f"=== 無効手(理由ごと。計 {sum(invalid.values())} 件) ==="]
    always_shown = ("schema_invalid", "agent_timeout", "output_truncated")  # 調査事項 R-3・台帳 C-53 の確かめなので、0 件でも出す
    lines += [f"  {reason}: {invalid.get(reason, 0)}" for reason in always_shown]
    lines += [f"  {reason}: {count}" for reason, count in sorted(invalid.items()) if reason not in always_shown]
    lines.append(
        "  (schema_invalid が 0 件なら、LLM の出力はすべて Plan・Move の検証(列挙値を含む)を通った。R-3「列挙値の制約が効くか」)"
    )
    return "\n".join(lines)


def format_judgement(judgement: Judgement) -> str:
    """DV-15 の判定の表示(項目ごとの合否と、実測の値・基準)。"""
    lines = ["", "=== DV-15 の判定(基準は config/params.toml の [agents.cost_targets]) ==="]
    lines += [f"  [{'合格' if check.passed else '不合格'}] {check.name}: {check.detail}" for check in judgement.checks]
    lines.append(f"  → {'合格' if judgement.passed else '不合格'}")
    return "\n".join(lines)


def write_record(
    run: DemoRun, directory: Path, *, targets: CostTargets | None = None, judgement: Judgement | None = None
) -> Path:
    """実行の記録(手の並び・送信ごとの使用量・物理の数・判定など)を JSONL で書く。1 行 1 件。交渉 ID・プロジェクト ID は書かない。

    targets を渡すと、単価の版・費用を持つ。judgement を渡すと、判定の結果(項目ごとの合否と値)を持つ。
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.strftime("%Y%m%dT%H%M%S_%f")
    path = directory / f"case{run.case}_{run.mode}_{stamp}.jsonl"
    limits = DEFAULT_VAULT_CONFIG.limits
    settings = load_agents_settings()
    header: dict[str, Any] = {
        "type": "run",
        "case": run.case,
        "mode": run.mode,
        "model": run.model,
        "model_id": run.model_id,
        "location": run.location,
        "started_at": run.started_at.isoformat(),
        "settings": {**settings, "explicit_cache": False},
        "settings_hash": settings_hash(run.model_id, settings),
        "limits": dataclasses.asdict(limits),
    }
    if targets is not None:
        header["price_version"] = targets.price_version
        header["prices_usd_per_million"] = {
            "input": targets.input_usd_per_million,
            "cached_input": targets.cached_input_usd_per_million,
            "output": targets.output_usd_per_million,
        }
    records: list[dict[str, Any]] = [header]
    for side, items in run.events.items():
        records += [{"type": "event", "side": side, **item.model_dump(mode="json")} for item in items]
    for call in run.calls:
        records.append(
            {
                "type": "call",
                "n": call.n,
                "role": call.role,
                "phase": call.phase,
                "seconds": round(call.seconds, 4),
                "kind": call.kind,
                "error": call.error,
                "usage": call.usage.model_dump(mode="json") if call.usage is not None else None,
                "output": call.output,
            }
        )
    summary: dict[str, Any] = {
        "type": "summary",
        "status": run.status,
        "end_reason": run.end_reason,
        "result": run.result.model_dump(mode="json") if run.result is not None else None,
        "counters": {side: counters.model_dump(mode="json") for side, counters in run.counters.items()},
        "invalid_moves_by_reason": dict(run.invalid_moves_by_reason()),
        "moves": run.moves_made,
        "physical_sends": len(run.calls),
        "successful_calls": run.successful_calls,
        "referee_counted_calls": run.referee_counted_calls,
        "elapsed_seconds": round(run.elapsed_seconds, 3),
        "timed_out": run.timed_out,
    }
    if targets is not None:
        summary["cost_usd"] = round(run_cost_usd(run, targets), 6)
    records.append(summary)
    if judgement is not None:
        records.append(
            {
                "type": "judgement",
                "passed": judgement.passed,
                "checks": [
                    {"name": check.name, "passed": check.passed, "detail": check.detail} for check in judgement.checks
                ],
            }
        )
    with path.open("x", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


# ----------------------------------------------------------------------
# コマンド
# ----------------------------------------------------------------------


def record_replay(run: DemoRun, path: Path) -> list[str]:
    """リプレイの記録(§8.4。形は scripts/replay_check.py)を path に書く。

    実行中に観測したイベントが、最後に金庫から読んだものと同じことと、記録が検査(replay_check)に通ることを確かめてから書く。
    通らないとき(判定に届かなかった実行など)は、何も書かずに、問題の一覧を返す(書けたら空)。
    """
    problems = [
        f"実行中に観測した{label}のイベントが、最後に金庫から読んだものと食い違っている"
        for side, label in _SIDE_LABELS.items()
        if [event.item for event in run.observed if event.side == side] != run.events[side]
    ]
    text = replay_check.dump_replay(run.case, run.mode, run.started_at.isoformat(), run.observed)
    problems += replay_check.parse_replay(text)[1]
    if problems:
        return problems
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return []


def play_replay(
    path: Path, speed: float, sleep: Callable[[float], None] = time.sleep, *, expected_case: int | None = None
) -> int:
    """リプレイの記録を検査してから、記録どおりの間隔(speed 倍)で、イベントを順に標準出力へ流す(§8.4・AC-09〜11)。

    金庫・agents・エミュレータは使わず、ネットワークにも出ない。出力は live の実行の報告と同じ形(側ごとのイベントの行と、結果)で、
    最初に「リプレイ」であることを出す。記録が不正なら(expected_case を渡したときは、記録のケースが違っても)、何も流さずに、
    問題を標準エラーに出して 1 を返す。
    """
    replay, problems = replay_check.check_file(path)
    if replay is not None and expected_case is not None and replay.case != expected_case:
        replay, problems = None, [f"記録はケース {replay.case} で、--case {expected_case} と違います"]
    if replay is None:
        print(f"リプレイを始められません: {replay_check.display_path(path)}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"run_demo: リプレイ(記録の再生。ケース {replay.case} / 記録の出所: {replay.source} / 記録日時: {replay.recorded_at})")
    print(f"倍率: x{speed:g}(記録どおりの間隔が x1)。金庫・agents・Gemini は呼ばない", flush=True)
    print("\n=== リプレイ: 双方の手の並び(側ごとの見え方) ===")
    origin = replay.events[0].observed_at
    for event in replay_check.iter_replay(replay, speed, sleep):
        tag = f"[{_SIDE_LABELS[event.side]}]" + ("  " if event.side == "employer" else "")  # 全角の幅をそろえる
        print(f"  +{event.observed_at - origin:6.1f} s {tag} {_format_event(event.item)}", flush=True)
    result = replay.events[-1].item.result  # 検査が、最後のイベントが result つきの最終結果であることを確かめている
    print("\n=== 結果 ===")
    print(f"見込み: {_LIKELIHOOD_LABELS[result.likelihood]}({result.likelihood})")
    print(f"組み合わせ: {format_package(result.package)}")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ケース別のデモを、金庫・agents・レフェリーをつないで通し、DV-15 の判定を行う。"
        "実行の記録をリプレイとして書き(--record)、流す(--replay)こともできる。"
    )
    parser.add_argument(
        "--case", type=int, help="ケース番号(fixtures/case{N}.toml)。--replay PATH のときは不要(記録のヘッダが持つ)"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="本物の Gemini(Vertex AI)で動かす。環境変数が要る")
    group.add_argument(
        "--scripted", action="store_true", help="Gemini を呼ばず、台本のエージェントで動かす(既定。このスクリプト自体の確かめ)"
    )
    parser.add_argument("--runs", type=int, default=1, help="続けて動かす回数(既定 1)。実行ごとに、新しい交渉・新しいデータベースで動かす")
    parser.add_argument(
        "--judge", action="store_true", help="DV-15 の基準(config/params.toml の [agents.cost_targets])で、実行ごとに判定する"
    )
    parser.add_argument(
        "--record", type=Path, metavar="PATH", help="実行中のイベントを、リプレイの記録(JSONL)として PATH に書く。--runs 1 のときだけ"
    )
    parser.add_argument(
        "--replay",
        nargs="?",
        const=True,
        type=Path,
        metavar="PATH",
        help="リプレイの記録を、記録どおりの間隔で流す(金庫・agents は使わない)。PATH を省くと、--case のケースの fixtures/replays/case{N}.jsonl",
    )
    parser.add_argument("--speed", type=float, help="--replay の倍率(既定 1.0。大きいほど速い)")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs は 1 以上にしてください")
    if args.speed is not None and not args.speed > 0:
        parser.error("--speed は 0 より大きい値にしてください")
    if args.replay is not None:
        if args.live or args.scripted or args.judge or args.record is not None or args.runs != 1:
            parser.error("--replay は記録を流すだけで、--live・--scripted・--runs・--judge・--record とは一緒に使えません")
        if args.replay is True and args.case is None:
            parser.error("--replay は、記録の PATH か --case(そのケースの fixtures/replays/case{N}.jsonl)が要ります")
    else:
        if args.case is None:
            parser.error("--case が要ります(--replay PATH のときは不要です)")
        if args.speed is not None:
            parser.error("--speed は --replay のときだけ使えます")
        if args.record is not None and args.runs != 1:
            parser.error("--record は --runs 1 のときだけ使えます(記録は 1 回の実行ごとに 1 ファイルです)")
    return args


def _mask_ids(text: str) -> str:
    return _ID_IN_TEXT.sub("<id>", text)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.replay is not None:
        path = replay_check.REPLAYS_DIRECTORY / f"case{args.case}.jsonl" if args.replay is True else args.replay
        return play_replay(path, 1.0 if args.speed is None else args.speed, expected_case=args.case)
    mode: Mode = "live" if args.live else "scripted"
    model, location = describe_backend(mode, os.environ)
    print(f"run_demo: ケース {args.case} / {mode}")
    print(f"使うモデル: {model}")
    print(f"場所: {location}", flush=True)

    if mode == "live":
        problem = check_live_environment(os.environ, args.case)
        if problem is not None:
            print(problem, file=sys.stderr)
            return 1
    try:
        fixture = load_case_fixture(args.case)
    except FileNotFoundError:
        print(f"エラー: fixtures/case{args.case}.toml がありません。", file=sys.stderr)
        return 1
    if mode == "scripted" and args.case not in _SCRIPTED_CASES:
        cases = "・".join(str(case) for case in _SCRIPTED_CASES)
        print(f"エラー: ケース {args.case} の台本のエージェントはありません(--scripted はケース {cases} だけ)。", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    mask_ids_in_logs()  # httpx などのログの URL に、交渉 ID が出ないようにする(§3.8)

    targets = load_cost_targets()
    outcomes: list[tuple[DemoRun, Judgement | None]] = []
    record_failed = False
    try:
        with firestore_emulator() as host:
            print(f"Firestore エミュレータ: 起動した({host})", flush=True)
            for index in range(args.runs):
                if args.runs > 1:
                    print(f"\n##### 実行 {index + 1}/{args.runs} #####", flush=True)
                project = f"demo-run-{uuid.uuid4().hex[:12]}"  # 実行ごとに、別のデータベース(前の実行の交渉・カウンタを引き継がない)
                vault_db = firestore.Client(project=project, database=VAULT_DATABASE)
                default_db = firestore.Client(project=project, database=DEFAULT_DATABASE)
                try:
                    run = asyncio.run(
                        run_demo(
                            fixture=fixture,
                            mode=mode,
                            vault_db=vault_db,
                            default_db=default_db,
                            on_send=_print_progress,
                            record_events=args.record is not None,
                        )
                    )
                finally:
                    vault_db.close()
                    default_db.close()
                judgement = judge(run, targets) if args.judge else None
                print(format_report(run, targets))
                if judgement is not None:
                    print(format_judgement(judgement))
                outcomes.append((run, judgement))
                try:
                    path = write_record(run, RUN_RECORD_DIRECTORY, targets=targets, judgement=judgement)
                    print(f"\n記録: {replay_check.display_path(path)}")
                except OSError as exc:  # 記録を書けなくても、終了コードは交渉の結果で決める
                    print(f"\n記録を書けなかった: {type(exc).__name__}", file=sys.stderr)
                if args.record is not None:
                    try:
                        replay_problems = record_replay(run, args.record)
                    except OSError as exc:
                        replay_problems = [f"書けなかった({type(exc).__name__})"]
                    if replay_problems:
                        record_failed = True
                        print("リプレイの記録を書かなかった:", file=sys.stderr)
                        for problem in replay_problems:
                            print(f"  - {problem}", file=sys.stderr)
                    else:
                        print(f"リプレイの記録: {replay_check.display_path(args.record)}(イベント {len(run.observed)} 件)")
        print("Firestore エミュレータ: 止めた")
    except Exception as exc:
        print(f"エラー: {type(exc).__name__}", file=sys.stderr)
        print(_mask_ids(traceback.format_exc()), file=sys.stderr)
        return 1

    code = _print_verdict(outcomes, judged=args.judge)
    if record_failed and code == 0:  # 記録を頼まれて書けなかったのは、失敗(記録を作るコマンドが、黙って成功しないように)
        print("リプレイの記録を書けなかったので、終了コード 1")
        return 1
    return code


def _print_verdict(outcomes: list[tuple[DemoRun, Judgement | None]], *, judged: bool) -> int:
    """全体の判定を表示して、終了コードを返す(モジュールの docstring の「終了コード」)。"""
    total = len(outcomes)
    if judged:
        passed = sum(1 for _, judgement in outcomes if judgement is not None and judgement.passed)
        code = 0 if passed == total else 1
        print(f"\nDV-15 の判定: {total} 回中 {passed} 回が合格 → 終了コード {code}")
        return code
    intended = sum(1 for run, _ in outcomes if ended_as_intended(run))
    code = 0 if intended == total else 1
    _, intended_label, missed_label = _INTENDED_ENDS.get(outcomes[0][0].case, _INTENDED_ENDS[1])
    if total == 1:
        print(f"判定: {intended_label if intended else missed_label} → 終了コード {code}")
    else:
        print(f"判定: {total} 回中 {intended} 回が{intended_label} → 終了コード {code}")
    return code


def _exit_at_signal(signum: int, frame) -> None:
    """Ctrl-C(SIGINT)・SIGTERM のときは、エミュレータを止めて、すぐ終わる。

    sys.exit で普通に終わろうとすると、プロセスが残った(SIGTERM から 40 秒以上たっても、Python が、作業用のスレッドの終了を
    待ち続けていた)。進行中の A2A の呼び出しも、cancel を受け付けない(RunTimeout を参照)。
    """
    for process in list(_running_emulators):
        _stop_emulator(process)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _exit_at_signal)
    signal.signal(signal.SIGINT, _exit_at_signal)
    sys.exit(main())
