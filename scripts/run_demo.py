"""ケース別のデモを、金庫・agents・レフェリーをつないで通し、DV-15 の判定を行うスクリプト(design.md §12.2 の DV-15。実装計画 ②・③-0)。

    uv run python scripts/run_demo.py --case 1 --live                       # 本物の ADK と Gemini(Vertex AI)で 1 回
    uv run python scripts/run_demo.py --case 1 --live --runs 2 --judge      # 2 回続けて DV-15 の基準で判定する(③-0 の完了条件)
    uv run python scripts/run_demo.py --case 1 --scripted [--runs N --judge]  # Gemini を呼ばず、台本のエージェントで(このスクリプト自体の確かめ)

流れ(1 回の実行)
1. Firestore エミュレータを起動する(tests/conftest.py と同じく、gcloud を使わず、JDK 21 で jar を直接起動する)。--runs N でも、
   起動は 1 回(実行ごとに、別のプロジェクト ID のデータベースを使う)。
2. ケースのフィクスチャ(fixtures/case{N}.toml)を、金庫(vault-db)のテンプレートに置く。
3. 金庫の app を、同じプロセスの中で ASGI のまま(httpx の ASGITransport)つなぐ。サービス間の認証(ID トークン)は使わない。
   --live は、agents の app も ASGI でつなぎ、設定ファイルのモデル(gemini-3.5-flash)で、本物の ADK が Gemini を呼ぶ。
   --scripted は、agents を通さず、台本のエージェント(tests/scripted_negotiators.py。計画・決定の形)をレフェリーの send_turn の
   差し込み口に直接入れる(agents の ADK・A2A の経路は、--live と agents のテストで確かめる)。
4. web のレフェリーで、デモの交渉(mode=demo)を 1 件作り、判定(judged)まで動かす。架空人物の途中確認は、フィクスチャの生の条件で答える
   (web.fictional_answerer.FixtureAnswerer)。レフェリーは、本番と同じく、送る前に物理の呼び出し数を `(default)`(エミュレータ)のカウンタで
   数える(web.llm_budget)。
5. 結果と診断を表示し、実行の記録を tmp/demo_runs/ に JSONL で残す。--judge なら、DV-15 の判定(下)も行う。エミュレータを止める。
   表示にも記録にも、交渉 ID などの ID と、プロジェクト ID は出さない。
   エージェントへの送信が終わるたびに、標準エラーへ進み具合を出す(本物の Gemini は、全体で数分かかり得る)。
   Ctrl-C・SIGTERM のときは、エミュレータを止めて、すぐ終わる。

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

終了コード: 既定は、合意(agreed)なら 0、それ以外は 1。--judge のときは、すべての実行が DV-15 の基準に合格すれば 0、1 つでも不合格なら 1。
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
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 先頭に src(プロジェクトのコード)を足す。tests/ は --scripted の台本のエージェントを読むときだけ足す(scripted_sender)。
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx  # noqa: E402
from google.cloud import firestore  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

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

# --scripted の最初の手(7 巡目のシミュレーション・tests/test_fixtures.py の 36 通りの始め方の 1 つ)。(候補者, 求人)。
# 台本のエージェントがあるのはケース 1 だけ。
_SCRIPTED_OPENINGS: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {
    1: (
        dict(salary=900, remote_days=3, night_duty=0, review_months=6),
        dict(salary=500, remote_days=0, night_duty=4, review_months=12),
    )
}
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


def scripted_sender(case: int) -> SendTurn:
    """--scripted の、Gemini の代わりのエージェント(ネットワークに出ない。agents を通さない)。

    tests/scripted_negotiators.py の台本のエージェント(指示文どおりの探し方。両側とも hybrid)。Gemini と同じく TurnInput だけを見て、
    計画(phase=plan)なら Plan、決定(phase=decide)なら Move を返す。使用量は固定の合成値(SCRIPTED_USAGE)。
    """
    if str(PROJECT_ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "tests"))
    from scripted_negotiators import Negotiator, ScriptedNegotiators, Strategy

    candidate_opening, employer_opening = _SCRIPTED_OPENINGS[case]
    return ScriptedNegotiators(
        Negotiator(Strategy("hybrid"), Package(**candidate_opening, **_SCRIPTED_CATEGORICAL)),
        Negotiator(Strategy("hybrid"), Package(**employer_opening, **_SCRIPTED_CATEGORICAL)),
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
) -> DemoRun:
    """デモの交渉を 1 件作り、レフェリーで判定(judged)まで動かす。vault_db・default_db は、エミュレータの vault-db・(default)。

    金庫の app は、同じプロセスの中で ASGI のままつなぐ(サービス間の認証は使わない)。--live は、agents の app も同じようにつなぐ。
    判定まで届かなかったとき(時間切れ)も、その時点の状態を診断の材料として返す。
    """
    started_at = dt.datetime.now(dt.timezone.utc)
    put_fixture_templates(vault_db, fixture)
    clock = SystemClock()
    store = VaultStore(db=vault_db, clock=clock, config=DEFAULT_VAULT_CONFIG)
    vault_transport = httpx.ASGITransport(app=create_vault_app(store))
    async with httpx.AsyncClient(transport=vault_transport, base_url="http://vault") as vault_http:
        vault = VaultClient(vault_http)
        created = await vault.create_negotiation(
            CreateNegotiationRequest(
                request_id=uuid.uuid4().hex,  # 毎回新しい乱数(同じ request_id は、すでに作った交渉を返すため)
                mode="demo",
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
            inner = scripted_sender(fixture.case)
            agents_context = contextlib.nullcontext()
        recorder = SendRecorder(inner, on_send)
        deps = RefereeDeps(
            vault=vault,
            send_turn=with_deadline(recorder, time.monotonic() + timeout_seconds),
            answerer=FixtureAnswerer(fixture),
            llm_budget=budget,
        )
        referee = Referee(NegotiationContext(nid=nid, mode="demo", candidate_principal_id=None), deps)
        started = time.perf_counter()
        timed_out = False
        with agents_context:
            try:
                await referee.run()
            except RunTimeout:
                timed_out = True
        elapsed = time.perf_counter() - started
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


def judge(run: DemoRun, targets: CostTargets) -> Judgement:
    """DV-15 の判定(モジュールの docstring)。実測の値だけで決め、手で直さない。"""
    checks: list[Check] = []

    agreed = run.end_reason == "agreed" and not run.timed_out
    checks.append(Check("agreed", agreed, f"金庫の終了理由: {run.end_reason or '(終了していない)'}"))

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
    lines += ["", "=== エージェントへの送信 ==="]
    lines.append(
        f"物理の送信 計 {len(calls)} 回(候補者 {by_role['candidate']}・求人 {by_role['employer']}。"
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


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ケース別のデモを、金庫・agents・レフェリーをつないで通し、DV-15 の判定を行う。")
    parser.add_argument("--case", type=int, required=True, help="ケース番号(fixtures/case{N}.toml)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true", help="本物の Gemini(Vertex AI)で動かす。環境変数が要る")
    group.add_argument(
        "--scripted", action="store_true", help="Gemini を呼ばず、台本のエージェントで動かす(このスクリプト自体の確かめ)"
    )
    parser.add_argument("--runs", type=int, default=1, help="続けて動かす回数(既定 1)。実行ごとに、新しい交渉・新しいデータベースで動かす")
    parser.add_argument(
        "--judge", action="store_true", help="DV-15 の基準(config/params.toml の [agents.cost_targets])で、実行ごとに判定する"
    )
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs は 1 以上にしてください")
    return args


def _mask_ids(text: str) -> str:
    return _ID_IN_TEXT.sub("<id>", text)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
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
    if mode == "scripted" and args.case not in _SCRIPTED_OPENINGS:
        print(f"エラー: ケース {args.case} の台本のエージェントはありません(--scripted はケース 1 だけ)。", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    mask_ids_in_logs()  # httpx などのログの URL に、交渉 ID が出ないようにする(§3.8)

    targets = load_cost_targets()
    outcomes: list[tuple[DemoRun, Judgement | None]] = []
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
                            fixture=fixture, mode=mode, vault_db=vault_db, default_db=default_db, on_send=_print_progress
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
                    print(f"\n記録: {path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path}")
                except OSError as exc:  # 記録を書けなくても、終了コードは交渉の結果で決める
                    print(f"\n記録を書けなかった: {type(exc).__name__}", file=sys.stderr)
        print("Firestore エミュレータ: 止めた")
    except Exception as exc:
        print(f"エラー: {type(exc).__name__}", file=sys.stderr)
        print(_mask_ids(traceback.format_exc()), file=sys.stderr)
        return 1

    return _print_verdict(outcomes, judged=args.judge)


def _print_verdict(outcomes: list[tuple[DemoRun, Judgement | None]], *, judged: bool) -> int:
    """全体の判定を表示して、終了コードを返す(モジュールの docstring の「終了コード」)。"""
    total = len(outcomes)
    if judged:
        passed = sum(1 for _, judgement in outcomes if judgement is not None and judgement.passed)
        code = 0 if passed == total else 1
        print(f"\nDV-15 の判定: {total} 回中 {passed} 回が合格 → 終了コード {code}")
        return code
    agreed = sum(1 for run, _ in outcomes if run.end_reason == "agreed")
    code = 0 if agreed == total else 1
    if total == 1:
        print(f"判定: {'合意(agreed)' if agreed else '合意に届かなかった'} → 終了コード {code}")
    else:
        print(f"判定: {total} 回中 {agreed} 回が合意(agreed) → 終了コード {code}")
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
