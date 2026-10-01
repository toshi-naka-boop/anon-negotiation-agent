"""ケース別のデモを、金庫・agents・レフェリーをつないで 1 回通すスクリプト(design.md §12.2 の DV-15。実装計画 ②)。

    uv run python scripts/run_demo.py --case 1 --live       # 本物の ADK と Gemini(Vertex AI)で
    uv run python scripts/run_demo.py --case 1 --scripted   # Gemini を呼ばず、台本のエージェントで(このスクリプト自体の確かめ)

流れ
1. Firestore エミュレータを起動する(tests/conftest.py と同じく、gcloud を使わず、JDK 21 で jar を直接起動する)。
2. ケースのフィクスチャ(fixtures/case{N}.toml)を、金庫(vault-db)のテンプレートに置く。
3. 金庫の app と agents の app を、同じプロセスの中で ASGI のまま(httpx の ASGITransport)つなぐ。サービス間の認証
   (ID トークン)は使わない。--live の agents は、設定ファイルのモデル(gemini-3.5-flash)で、本物の ADK が Gemini を呼ぶ。
4. web のレフェリーで、デモの交渉(mode=demo)を 1 件作り、判定(judged)まで動かす。架空人物の途中確認は、
   フィクスチャの生の条件で答える(web.fictional_answerer.FixtureAnswerer)。
5. 結果と診断を表示し、実行の記録を tmp/demo_runs/ に JSONL で残す。エミュレータを止める。
   表示にも記録にも、交渉 ID などの ID と、プロジェクト ID は出さない。
   LLM の呼び出しが終わるたびに、標準エラーへ進み具合を出す(本物の Gemini は、全体で数分かかり得る)。
   Ctrl-C・SIGTERM のときは、エミュレータを止めて、すぐ終わる。

終了コード: 合意(agreed)なら 0、それ以外は 1。判定(judged)に届かないまま時間の上限(RUN_TIMEOUT_SECONDS)を過ぎたときも 1
(その時点までの状態を、診断として表示・記録する)。

--live には、ADK と google-genai の標準の環境変数が要る。プロジェクト ID は、コードにも設定ファイルにも書かない。
    GOOGLE_GENAI_USE_VERTEXAI=TRUE
    GOOGLE_CLOUD_PROJECT=<プロジェクト ID>
    GOOGLE_CLOUD_LOCATION=global
足りなければ、何も呼ばずに(エミュレータも起動せずに)説明を出して止まる。本物の GCP に接続するのは、--live の agents が
Gemini を呼ぶときだけ。Firestore は、エミュレータだけにつなぐ(demo- で始まるプロジェクト ID を使う)。--scripted は、
ネットワークに出ない(エミュレータは 127.0.0.1)。

--scripted の LLM は、tests/scripted_negotiators.py の台本のエージェント(指示文どおりの探し方)。ADK の Runner からは Gemini と
同じ口で呼ばれるので、A2A の受信口・ADK・検証・レフェリーまでの経路は --live と同じ。
"""

import argparse
import asyncio
import collections
import contextlib
import dataclasses
import datetime as dt
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
import traceback
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 先頭に src(プロジェクトのコード)を足す。tests/ は --scripted の台本のエージェントを読むときだけ足す(_scripted_llm)。
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_request import LlmRequest  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.plugins.base_plugin import BasePlugin  # noqa: E402
from google.cloud import firestore  # noqa: E402
from google.genai import types  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

import agents.client as agents_client  # noqa: E402
from agents.app import create_app as create_agents_app  # noqa: E402
from agents.config import DEFAULT_AGENTS_CONFIG  # noqa: E402
from negotiation_core import Package, Side, TurnInput  # noqa: E402
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
from web.app import bind_agents_client  # noqa: E402
from web.fictional_answerer import FixtureAnswerer  # noqa: E402
from web.referee import AgentRole, NegotiationContext, Referee, RefereeDeps, SendTurn  # noqa: E402
from web.vault_client import VaultClient  # noqa: E402

logger = logging.getLogger("run_demo")

Mode = Literal["live", "scripted"]

# 実行の記録(JSONL)を置く場所。tmp/ は .gitignore に入っている。
RUN_RECORD_DIRECTORY = PROJECT_ROOT / "tmp" / "demo_runs"
# 交渉 1 件を、判定(judged)まで動かす時間の上限(秒)。見回りは動かさないので、止まったままの交渉をここで打ち切る。
# 上限を過ぎたら、次の LLM の呼び出しを始めずに止める(進行中の呼び出しは、終わるまで待つ。1 回は最長 60 秒)。
# 上限+60 秒が、Bash の 10 分の上限に収まるようにしてある。
RUN_TIMEOUT_SECONDS = 480.0

# Firestore エミュレータの起動(tests/conftest.py と同じ値)。
_JAVA_BIN = "/opt/homebrew/opt/openjdk@21/bin/java"
_EMULATOR_JAR = "/opt/homebrew/share/google-cloud-sdk/platform/cloud-firestore-emulator/cloud-firestore-emulator.jar"
_READY_MARKER = "Dev App Server is now running"
_STARTUP_TIMEOUT_SECONDS = 30

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

    本物の GCP には接続しない: 呼び出し側が Firestore のクライアントを作る前に、FIRESTORE_EMULATOR_HOST をここで
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
# LLM(--live は本物の Gemini、--scripted は台本)と、呼び出しの記録
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LlmCall:
    """LLM の呼び出し 1 回の記録(診断用)。"""

    n: int
    role: str
    seconds: float
    error: str | None  # 失敗したときだけ。型名と HTTP のステータス(メッセージは持たない)
    input_tokens: int | None
    output_tokens: int | None
    thought_tokens: int | None
    finish_reason: str | None
    output_text: str | None  # モデルの出力の JSON(JSONL にだけ書く)


def _describe_error(error: BaseException) -> str:
    """LLM の失敗を、型名と(あれば)HTTP のステータスで表す。メッセージは書かない(入力の値が入り得るため)。"""
    parts = [type(error).__name__]
    code, status = getattr(error, "code", None), getattr(error, "status", None)
    if isinstance(code, int):
        parts.append(str(code))
    if isinstance(status, str) and status:
        parts.append(status)
    return " ".join(parts)


class LlmCallRecorder(BasePlugin):
    """ADK のプラグイン。モデルを呼ぶたびに、回数・所要時間・結果を記録する(診断用。呼び出し自体は変えない)。

    agents の Runner(app.state.runners)に登録して使う。--live の本物の Gemini の呼び出しも、--scripted の台本も、
    ADK からは同じ口なので、同じ記録が取れる。
    """

    def __init__(self, on_call: Callable[[LlmCall], None] | None = None) -> None:
        super().__init__(name="demo_llm_call_recorder")
        self.calls: list[LlmCall] = []
        self._on_call = on_call  # 呼び出しが終わるたびに呼ぶ(進み具合の表示用)
        self._started: dict[str, float] = {}  # invocation_id → 開始時刻

    # ADK は、プラグインの例外を、モデルの呼び出しの失敗にする。記録の失敗で、診断の対象(LLM の呼び出し)を壊さないよう、
    # 3 つの口はどれも、例外を外に出さない(失敗は警告に残す)。
    async def before_model_callback(self, *, callback_context, llm_request):
        self._safely(self._start, callback_context)
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        self._safely(self._finish_with_response, callback_context, llm_response)
        return None

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        self._safely(self._finish, callback_context, error=_describe_error(error))
        return None

    def _safely(self, record: Callable[..., None], *args, **kwargs) -> None:
        try:
            record(*args, **kwargs)
        except Exception as exc:
            logger.warning("LLM call recorder failed: %s", type(exc).__name__)

    def _start(self, callback_context) -> None:
        self._started[callback_context.invocation_id] = time.perf_counter()

    def _finish_with_response(self, callback_context, llm_response) -> None:
        text = None
        if llm_response.content is not None and llm_response.content.parts:
            text = "".join(part.text for part in llm_response.content.parts if part.text and not part.thought)
        self._finish(
            callback_context,
            error=f"error_code {llm_response.error_code}" if llm_response.error_code else None,
            usage=llm_response.usage_metadata,
            finish_reason=llm_response.finish_reason.name if llm_response.finish_reason else None,
            output_text=text[:2000] if text else None,
        )

    def _finish(self, callback_context, *, error, usage=None, finish_reason=None, output_text=None) -> None:
        now = time.perf_counter()
        started = self._started.pop(callback_context.invocation_id, now)
        call = LlmCall(
            n=len(self.calls) + 1,
            role=callback_context.agent_name.removesuffix("_agent"),
            seconds=now - started,
            error=error,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=usage.candidates_token_count if usage else None,
            thought_tokens=usage.thoughts_token_count if usage else None,
            finish_reason=finish_reason,
            output_text=output_text,
        )
        self.calls.append(call)
        if self._on_call is not None:
            self._on_call(call)


class ScriptedLlm(BaseLlm):
    """--scripted の、Gemini の代わりの LLM(ネットワークに出ない)。

    LLM に渡る入力(TurnInput の JSON)だけを見て、Move の JSON を返す(台本のエージェントは、Gemini と同じく、
    TurnInput 以外の記憶を持たない)。
    """

    model: str = "scripted"
    behavior: Callable[[TurnInput], dict]

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
        turn_input = TurnInput.model_validate_json(llm_request.contents[-1].parts[0].text)
        move = {key: value for key, value in self.behavior(turn_input).items() if value is not None}
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(move))]))


def _scripted_llm(case: int) -> ScriptedLlm:
    """tests/scripted_negotiators.py の台本のエージェント(指示文どおりの探し方。両側とも hybrid)で動く LLM。"""
    if str(PROJECT_ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "tests"))
    from scripted_negotiators import Negotiator, Strategy, decide

    candidate_opening, employer_opening = _SCRIPTED_OPENINGS[case]
    negotiators = {
        "candidate": Negotiator(Strategy("hybrid"), Package(**candidate_opening, **_SCRIPTED_CATEGORICAL)),
        "employer": Negotiator(Strategy("hybrid"), Package(**employer_opening, **_SCRIPTED_CATEGORICAL)),
    }
    return ScriptedLlm(behavior=lambda turn_input: decide(turn_input, negotiators[turn_input.side]))


def build_agents_app(mode: Mode, case: int, recorder: LlmCallRecorder) -> Starlette:
    """agents の app を作り、LLM の呼び出しの記録(recorder)を登録する。

    --live は、設定ファイルのモデル名のまま(ADK が最初の呼び出しのときに、環境変数の Vertex AI につなぐ)。
    --scripted は、台本の LLM を差し込む(Gemini にはつながない)。
    """
    app = create_agents_app() if mode == "live" else create_agents_app(model=_scripted_llm(case))
    for runner in app.state.runners.values():
        runner.plugin_manager.register_plugin(recorder)
    return app


class RunTimeout(BaseException):
    """時間の上限を過ぎた(run_demo が受け止める)。

    BaseException にして、レフェリーの「約束にない例外は、その手番を無効手にする」処理に飲み込まれないようにする。
    asyncio の cancel で止めないのは、金庫・agents を同じプロセスの ASGI でつなぐと、進行中の A2A の呼び出しが cancel を
    受け付けないため(呼び出しの中の LLM が終わるまで、最長 45 秒、cancel が握りつぶされる。実際の HTTP では、すぐ効く)。
    """


def with_deadline(send_turn: SendTurn, deadline: float) -> SendTurn:
    """send_turn を、deadline(time.monotonic の時刻)を過ぎたら、呼ばずに RunTimeout を投げるものにする。"""

    async def send(role: AgentRole, turn_input: TurnInput, *, nid: str, timeout_s: float) -> dict:
        if time.monotonic() >= deadline:
            raise RunTimeout
        return await send_turn(role, turn_input, nid=nid, timeout_s=timeout_s)

    return send


@contextlib.contextmanager
def agents_over_asgi(app: Starlette) -> Iterator[None]:
    """web(レフェリー)から agents への A2A の呼び出しを、ネットワークを通さず、agents の app に ASGI のままつなぐ。

    agents.client.send_turn が HTTP クライアントを作る口(_open_http_client)を差し替える(tests/test_agent_client.py と
    同じ口)。終わったら元に戻す。
    """
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
    """1 回の実行の結果と、診断の材料。nid は金庫から読むためだけに持ち、表示にも記録にも出さない。"""

    case: int
    mode: Mode
    model: str
    location: str
    started_at: dt.datetime
    nid: str
    status: str
    end_reason: str | None  # 金庫の中の終了理由(診断用。外には出ない値)
    result: NegotiationResult | None
    events: dict[Side, list[EventViewItem]]  # 側ごとの見え方
    counters: dict[Side, SideCounters]
    llm_calls: list[LlmCall]
    elapsed_seconds: float
    timed_out: bool
    timeout_seconds: float

    def invalid_moves_by_reason(self) -> collections.Counter:
        """無効手の数(理由ごと。両側の合計)。"""
        return collections.Counter(
            item.reason for items in self.events.values() for item in items if item.kind == "invalid"
        )


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
    timeout_seconds: float = RUN_TIMEOUT_SECONDS,
    on_llm_call: Callable[[LlmCall], None] | None = None,
) -> DemoRun:
    """デモの交渉を 1 件作り、レフェリーで判定(judged)まで動かす。vault_db は、エミュレータの vault-db。

    金庫の app と agents の app は、同じプロセスの中で ASGI のままつなぐ(サービス間の認証は使わない)。
    判定まで届かなかったとき(時間切れ)も、その時点の状態を診断の材料として返す。
    """
    started_at = dt.datetime.now(dt.timezone.utc)
    put_fixture_templates(vault_db, fixture)
    store = VaultStore(db=vault_db, clock=SystemClock(), config=DEFAULT_VAULT_CONFIG)
    recorder = LlmCallRecorder(on_llm_call)
    agents_app = build_agents_app(mode, fixture.case, recorder)
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
        started = time.perf_counter()
        deps = RefereeDeps(
            vault=vault,
            send_turn=with_deadline(bind_agents_client(_AGENTS_BASE_URL), time.monotonic() + timeout_seconds),
            answerer=FixtureAnswerer(fixture),
        )
        referee = Referee(NegotiationContext(nid=nid, mode="demo", candidate_principal_id=None), deps)
        timed_out = False
        with agents_over_asgi(agents_app):
            try:
                await referee.run()
            except RunTimeout:
                timed_out = True
        elapsed = time.perf_counter() - started
        events = {side: await vault.get_events(nid, side) for side in _SIDE_LABELS}

    # 診断のため、金庫の文書を直接読む(終了理由は、金庫の中だけの値で、API には出ない)。
    document = model_from_firestore(NegotiationDocument, store._negotiation_ref(nid).get().to_dict())
    model, location = describe_backend(mode, os.environ)
    return DemoRun(
        case=fixture.case,
        mode=mode,
        model=model,
        location=location,
        started_at=started_at,
        nid=nid,
        status=document.status,
        end_reason=document.end_reason,
        result=document.result,
        events=events,
        counters={"candidate": document.counters.candidate, "employer": document.counters.employer},
        llm_calls=recorder.calls,
        elapsed_seconds=elapsed,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
    )


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


def _call_outcome(call: LlmCall) -> str:
    return "ok" if call.error is None else f"失敗 {call.error}"


def _print_progress(call: LlmCall) -> None:
    """LLM の呼び出しが終わるたびの進み具合(標準エラー。本物の Gemini は、全体で数分かかり得る)。"""
    print(f"  LLM #{call.n} {call.role} {call.seconds:.2f} s {_call_outcome(call)}", file=sys.stderr, flush=True)


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


def format_report(run: DemoRun) -> str:
    """結果と診断の表示(ID は出さない)。"""
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

    calls = run.llm_calls
    failed = [call for call in calls if call.error is not None]
    by_role = collections.Counter(call.role for call in calls)
    lines += ["", "=== LLM の呼び出し ==="]
    lines.append(
        f"計 {len(calls)} 回(候補者 {by_role['candidate']}・求人 {by_role['employer']}。"
        f"成功 {len(calls) - len(failed)}・失敗 {len(failed)})"
    )
    for call in calls:
        tokens = ""
        if call.input_tokens is not None or call.output_tokens is not None:
            tokens = f"  トークン 入力 {call.input_tokens} / 出力 {call.output_tokens} / 思考 {call.thought_tokens}"
        lines.append(f"  #{call.n:<3} {call.role:<9} {call.seconds:6.2f} s  {_call_outcome(call)}{tokens}")
    if calls:
        seconds = [call.seconds for call in calls]
        lines.append(f"  合計 {sum(seconds):.1f} s / 平均 {sum(seconds) / len(seconds):.2f} s / 最長 {max(seconds):.2f} s")

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
    always_shown = ("schema_invalid", "agent_timeout")  # 調査事項 R-3 の確かめなので、0 件でも出す
    lines += [f"  {reason}: {invalid.get(reason, 0)}" for reason in always_shown]
    lines += [f"  {reason}: {count}" for reason, count in sorted(invalid.items()) if reason not in always_shown]
    lines.append(
        "  (schema_invalid が 0 件なら、LLM の出力はすべて Move の検証(列挙値を含む)を通った。R-3「列挙値の制約が効くか」)"
    )
    return "\n".join(lines)


def write_record(run: DemoRun, directory: Path) -> Path:
    """実行の記録(手の並びなど)を JSONL で書く。1 行 1 件。交渉 ID・プロジェクト ID は書かない。"""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.strftime("%Y%m%dT%H%M%S_%f")
    path = directory / f"case{run.case}_{run.mode}_{stamp}.jsonl"
    limits = DEFAULT_VAULT_CONFIG.limits
    records: list[dict[str, Any]] = [
        {
            "type": "run",
            "case": run.case,
            "mode": run.mode,
            "model": run.model,
            "location": run.location,
            "started_at": run.started_at.isoformat(),
            "limits": dataclasses.asdict(limits),
        }
    ]
    for side, items in run.events.items():
        records += [{"type": "event", "side": side, **item.model_dump(mode="json")} for item in items]
    records += [
        {"type": "llm_call", **dataclasses.asdict(call), "seconds": round(call.seconds, 4)} for call in run.llm_calls
    ]
    records.append(
        {
            "type": "summary",
            "status": run.status,
            "end_reason": run.end_reason,
            "result": run.result.model_dump(mode="json") if run.result is not None else None,
            "counters": {side: counters.model_dump(mode="json") for side, counters in run.counters.items()},
            "invalid_moves_by_reason": dict(run.invalid_moves_by_reason()),
            "llm_calls": len(run.llm_calls),
            "elapsed_seconds": round(run.elapsed_seconds, 3),
            "timed_out": run.timed_out,
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
    parser = argparse.ArgumentParser(description="ケース別のデモを、金庫・agents・レフェリーをつないで 1 回通す(DV-15)。")
    parser.add_argument("--case", type=int, required=True, help="ケース番号(fixtures/case{N}.toml)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true", help="本物の Gemini(Vertex AI)で動かす。環境変数が要る")
    group.add_argument(
        "--scripted", action="store_true", help="Gemini を呼ばず、台本のエージェントで動かす(このスクリプト自体の確かめ)"
    )
    return parser.parse_args(argv)


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
    if mode == "scripted":
        # 台本の LLM は、トークン数を返さない。ADK が呼び出しのたびに出す警告は、ここでは要らない。
        logging.getLogger("google_adk.google.adk.telemetry._metrics").setLevel(logging.ERROR)

    try:
        with firestore_emulator() as host:
            print(f"Firestore エミュレータ: 起動した({host})", flush=True)
            vault_db = firestore.Client(project=f"demo-run-{uuid.uuid4().hex[:12]}", database=VAULT_DATABASE)
            try:
                run = asyncio.run(
                    run_demo(fixture=fixture, mode=mode, vault_db=vault_db, on_llm_call=_print_progress)
                )
            finally:
                vault_db.close()
        print("Firestore エミュレータ: 止めた")
        print(format_report(run))
    except Exception as exc:
        print(f"エラー: {type(exc).__name__}", file=sys.stderr)
        print(_mask_ids(traceback.format_exc()), file=sys.stderr)
        return 1
    try:
        path = write_record(run, RUN_RECORD_DIRECTORY)
        print(f"\n記録: {path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path}")
    except OSError as exc:  # 記録を書けなくても、終了コードは交渉の結果で決める
        print(f"\n記録を書けなかった: {type(exc).__name__}", file=sys.stderr)
    agreed = run.end_reason == "agreed"
    print(f"判定: {'合意(agreed)' if agreed else '合意に届かなかった'} → 終了コード {0 if agreed else 1}")
    return 0 if agreed else 1


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
