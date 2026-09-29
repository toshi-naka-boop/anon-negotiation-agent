"""web(レフェリー・見回り)のテストで共通に使う部品。

`test_` で始まらないので pytest には収集されない(tests/vault_helpers.py と同じ扱い)。
本物の LLM・GCP には接続しない: エージェントはスタブ、金庫は本物の vault の app を ASGI のまま
(ネットワークを通さず)つなぐ。時計は注入でき、テストは sleep しない(FakeSleep)。
"""

import asyncio
import datetime as dt
import inspect
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from negotiation_core import AttackerTurnInput, Move, Package, Policy, TurnInput
from vault.clock import FixedClock
from vault.models import EmployerRule
from vault.templates import put_template
from vault_helpers import (
    demo_create_request,
    live_create_request,
    make_employer_template,
    new_id,
    put_candidate_and_employer_templates,
    put_candidate_policy,
)
from web.config import DEFAULT_WEB_CONFIG, RefereeConfig
from web.referee import (
    NegotiationContext,
    Referee,
    RefereeDeps,
    RefereeManager,
    StepOutcome,
)
from web.stages import StageStore
from web.sweeper import Sweeper
from web.vault_client import VaultClient

# --- エージェントのスタブ ---


class ScriptExhausted(BaseException):
    """台本を使い切ったのに、エージェントが呼ばれた。

    BaseException にしてあるのは、レフェリーの「約束にない例外は無効手にする」処理に
    飲み込まれず、テストの誤りとして必ず表に出すため。
    """


class SimulatedCrash(BaseException):
    """web が落ちたことの再現(飲み込まれないよう BaseException)。"""


def move_dict(move: str, package: Package | None = None) -> dict:
    """エージェントが返す Move の dict(Move で検証済みの形)。"""
    return Move(schema="move/v1", move=move, package=package).model_dump(mode="json", by_alias=True)


@dataclass
class AgentCall:
    """エージェントの呼び出し 1 回の記録。"""

    role: str
    nid: str
    timeout_s: float
    turn_input: TurnInput | AttackerTurnInput


class ScriptedAgents:
    """SendTurn の形のスタブ。role ごとの台本を先頭から 1 つずつ消費する。

    台本の要素は次のどれか。
    - dict: そのまま返す(スキーマ違反の dict を返して検証を試すこともできる)
    - 例外のクラス・インスタンス: 投げる(ConnectionError・TimeoutError・ValueError など)
    - 関数: 呼び出しの記録(AgentCall)を渡して呼び、その戻り値を上の規則で扱う(awaitable も可)
    """

    def __init__(self) -> None:
        self._scripts: dict[str, deque] = defaultdict(deque)
        self.calls: list[AgentCall] = []

    def script(self, role: str, *items) -> "ScriptedAgents":
        self._scripts[role].extend(items)
        return self

    def calls_for(self, role: str) -> list[AgentCall]:
        return [call for call in self.calls if call.role == role]

    async def __call__(self, role, turn_input, *, nid, timeout_s) -> dict:
        call = AgentCall(role=role, nid=nid, timeout_s=timeout_s, turn_input=turn_input)
        self.calls.append(call)
        queue = self._scripts[role]
        if not queue:
            raise ScriptExhausted(f"no scripted move left for role={role!r}")
        item = queue.popleft()
        if callable(item) and not isinstance(item, type) and not isinstance(item, dict):
            item = item(call)
            if inspect.isawaitable(item):
                item = await item
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        return item


class Blocker:
    """エージェントの呼び出しを、止められるまで待たせる台本の要素(web が呼び出しの途中で落ちた状態の再現)。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def __call__(self, call: AgentCall) -> dict:
        self.started.set()
        await asyncio.Event().wait()  # 誰も set しない。タスクの cancel でだけ抜ける
        raise AssertionError("unreachable")


class ScriptedAnswerer:
    """FictionalAnswerer の形のスタブ。常に同じ回答を返し、呼ばれた内容を記録する。"""

    def __init__(self, answer: str = "accept") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, Package]] = []

    async def __call__(self, *, nid, side, package):
        self.calls.append((nid, side, package))
        return self.answer


class FakeSleep:
    """sleep の代わり。実際には待たない(テストは sleep しない)。

    通常は、待ち時間を記録して、他のタスクに順番を譲るだけで、すぐ戻る。
    - advance=True: 待ち時間の分だけ時計を進める(エージェント呼び出しの上限の確認用)。
    - hook: 待つたびに呼ぶ(待っている間に外から起きる操作の再現用。awaitable も可)。
    - blocking=True: tick() が呼ばれるまで戻らない。背景のタスクが「待つ」場面で、
      空回りさせずに、テストが 1 回ずつ進めるため(待ちが起きたことは wait_for_calls で確かめる)。
    """

    def __init__(self, clock: FixedClock | None = None, *, advance: bool = False) -> None:
        self._clock = clock
        self._advance = advance
        self.blocking = False
        self.calls: list[float] = []
        self.hook: Callable | None = None
        self._released = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._advance and self._clock is not None:
            self._clock.advance(dt.timedelta(seconds=seconds))
        if self.hook is not None:
            result = self.hook(self)
            if inspect.isawaitable(result):
                await result
        if self.blocking:
            await self._released.wait()
        else:
            await asyncio.sleep(0)

    def tick(self) -> None:
        """今 blocking で待っているタスクを、すべて 1 回だけ進める。"""
        released = self._released
        self._released = asyncio.Event()
        released.set()

    async def wait_for_calls(self, count: int, *, timeout: float = 10) -> None:
        """sleep が count 回呼ばれるまで(=背景のタスクが待ちに入るまで)、他のタスクに譲りながら待つ。"""

        async def _poll() -> None:
            while len(self.calls) < count:
                await asyncio.sleep(0)

        await asyncio.wait_for(_poll(), timeout=timeout)


class SpyVault:
    """moves のリクエストを記録してから、そのまま本物の金庫クライアントに渡す。"""

    def __init__(self, inner: VaultClient) -> None:
        self._inner = inner
        self.move_requests: list = []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def post_move(self, nid, request):
        self.move_requests.append(request)
        return await self._inner.post_move(nid, request)


class CrashAfterMove:
    """N 回目の moves が金庫にコミットされた直後に、web が落ちたことを再現する金庫クライアント。

    金庫の操作は済んでいるのに、応答はレフェリーに処理されない(SimulatedCrash が飛ぶ)。
    """

    def __init__(self, inner: VaultClient, crash_on_move_number: int) -> None:
        self._inner = inner
        self._crash_on = crash_on_move_number
        self._moves = 0

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def post_move(self, nid, request):
        response = await self._inner.post_move(nid, request)
        self._moves += 1
        if self._moves == self._crash_on:
            raise SimulatedCrash
        return response


# --- web の環境(レフェリー・見回り・段階開示の状態) ---


@dataclass
class WebEnv:
    """テスト用の web 一式。restart() で「web が落ちて、起動し直した」状態を作れる。"""

    store: object
    clock: FixedClock
    vault: object  # VaultClient(または CrashAfterMove)
    default_db: object
    agents: ScriptedAgents
    sleep: FakeSleep
    answerer: ScriptedAnswerer | None
    config: RefereeConfig
    attacker_instruction: Callable[[str], str] | None = None
    stages: StageStore = field(init=False)
    deps: RefereeDeps = field(init=False)
    manager: RefereeManager = field(init=False)
    sweeper: Sweeper = field(init=False)

    def __post_init__(self) -> None:
        self.stages = StageStore(self.default_db, self.clock)
        self._build()

    def _build(self) -> None:
        self.deps = RefereeDeps(
            vault=self.vault,
            send_turn=self.agents,
            clock=self.clock,
            sleep=self.sleep,
            config=self.config,
            answerer=self.answerer,
            attacker_instruction=self.attacker_instruction,
        )
        self.manager = RefereeManager(self.deps)
        self.sweeper = Sweeper(
            vault=self.vault, stages=self.stages, referees=self.manager, clock=self.clock, sleep=self.sleep
        )

    def configure(self, **changes) -> None:
        """answerer・attacker_instruction・config などを差し替えて、レフェリーの管理と見回りを作り直す。"""
        for name, value in changes.items():
            if not hasattr(self, name):
                raise AttributeError(name)
            setattr(self, name, value)
        self._build()

    async def restart(self, vault=None) -> None:
        """web が落ちて起動し直した状態にする: 動いていたタスクをすべて止め、管理と見回りを作り直す。

        金庫の状態と (default) の文書は、そのまま残る(新しいプロセスが読み直す)。
        """
        await self.manager.stop_all()
        if vault is not None:
            self.vault = vault
        self._build()

    def referee(self, nid: str, *, mode: str = "demo", candidate_principal_id: str | None = None) -> Referee:
        """1 手ずつ進めるための、タスクにしていないレフェリー。"""
        context = NegotiationContext(nid=nid, mode=mode, candidate_principal_id=candidate_principal_id)
        return Referee(context, self.deps)


def make_web_env(
    store,
    clock: FixedClock,
    vault,
    default_db,
    *,
    agents: ScriptedAgents | None = None,
    answerer: ScriptedAnswerer | None = None,
    config: RefereeConfig | None = None,
) -> WebEnv:
    return WebEnv(
        store=store,
        clock=clock,
        vault=vault,
        default_db=default_db,
        agents=agents if agents is not None else ScriptedAgents(),
        sleep=FakeSleep(clock),
        answerer=answerer,
        config=config if config is not None else DEFAULT_WEB_CONFIG.referee,
    )


async def drive(referee: Referee, *, max_steps: int = 40) -> list[StepOutcome]:
    """FINISHED になるまで step() を繰り返し、結果の並びを返す(終わらなければ失敗)。"""
    outcomes: list[StepOutcome] = []
    for _ in range(max_steps):
        outcome = await referee.step()
        outcomes.append(outcome)
        if outcome is StepOutcome.FINISHED:
            return outcomes
    raise AssertionError(f"referee did not finish within {max_steps} steps: {outcomes}")


# --- 金庫に交渉を作る(同期。web の作成の流れは 1d-2 なので、テストは金庫に直接作る) ---


def create_demo_negotiation(
    store,
    *,
    candidate_policy: Policy | None = None,
    employer_rules: list[EmployerRule] | None = None,
    mode: str = "demo",
    **template_kwargs,
) -> str:
    """架空人物どうしの交渉を作って nid を返す(mode は demo・attack)。"""
    candidate_template, employer_template = put_candidate_and_employer_templates(
        store._db, candidate_policy=candidate_policy, employer_rules=employer_rules, **template_kwargs
    )
    result = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id, mode=mode)
    )
    assert result.status == "created"
    return result.nid


def create_live_negotiation(
    store,
    *,
    candidate_policy: Policy | None = None,
    employer_rules: list[EmployerRule] | None = None,
    principal_id: str | None = None,
) -> tuple[str, str]:
    """本物の候補者 対 架空の求人の交渉を作って (nid, 依頼者 ID) を返す。"""
    pid = principal_id or new_id("principal")
    put_candidate_policy(store, pid, policy=candidate_policy)
    employer_template = make_employer_template(rules=employer_rules)
    put_template(store._db, employer_template)
    result = store.create_negotiation(live_create_request(pid, employer_template.template_id))
    assert result.status == "created"
    return result.nid, pid
