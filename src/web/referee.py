"""レフェリー(design.md §4.1)。交渉ごとに 1 つの asyncio のタスクとして動き、LLM の呼び出しを担う。

秘密に触れる判断・状態の遷移・記録は、すべて金庫に任せる。レフェリーがするのは次だけ。
- 手番の側について、金庫の view とイベント列から TurnInput を組み立てる(web.turn_input)。
- エージェントを呼び、返ってきた dict を negotiation_core.schema.Move として検証する。
- expected_version を付けて金庫の moves に登録する(409 なら、状態を読み直してから進める)。
- 検証に失敗したとき・エージェントが応答しないときは、move=invalid として登録する。
- 途中確認(awaiting_principal)・一時停止中は、金庫の状態が変わるまで待つ。

時刻と待ち時間は注入できる(clock・sleep)ので、テストは sleep せずに 1 手ずつ進められる
(step())。実際のタスクの起動・作り直しは RefereeManager と見回り(web.sweeper)が行う。

本物の候補者の交渉では、金庫への操作を 1 回ごとに、その依頼者のロックの下で行う(台帳 I-4。
RefereeDeps.locks を渡したとき。エージェントを呼んでいる間はロックを持たない)。

待って読み直す間隔は、既定では wait_poll_interval_seconds(暫定 2 秒)。ただし金庫が、送り直しても直らないエラー(404・409
以外の 4xx。422 など)を返した後は、見回りの間隔(暫定 60 秒。client_error_wait_seconds)にする(台帳 L10-1)。読み直すたびに
エージェント(LLM)を呼ぶので、直らないエラーで 2 秒ごとに呼び直すと、手番の期限で終わるまでの間に、LLM の呼び出しを使い果たす。

ログには、側・手の種類・理由(列挙値)・例外の型名・金庫の HTTP ステータスだけを書く。交渉 ID・依頼者 ID・
組み合わせの値は書かない(§3.8。台帳 X-40: ログは本人の削除の後も残り、ID から交渉の時刻・失敗の理由・側を
たどれてしまう)。例外の型名だけを書くのは、検証エラーのメッセージに入力値が含まれるため。タスクの名前にも、ID を入れない。
"""

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from negotiation_core import AttackerTurnInput, Move, Package, Side, TurnInput

from vault.api_models import MoveRequest, NegotiationViewResponse, PrincipalAnswerRequest
from vault.clock import Clock, SystemClock
from vault.models import NegotiationMode, PrincipalAnswerKind, RegisteredInvalidReason

from web.config import DEFAULT_WEB_CONFIG, RefereeConfig
from web.locks import PrincipalLocks, PrincipalScopedVault
from web.turn_input import build_turn_input, to_attacker_turn_input
from web.vault_client import (
    VaultClient,
    VaultClientError,
    VaultConflictError,
    VaultNotFoundError,
    VaultUnavailableError,
)

_log = logging.getLogger(__name__)

AgentRole = Literal["candidate", "employer", "attacker"]
Sleep = Callable[[float], Awaitable[None]]

# package を伴って金庫へ登録する手(negotiation_core.Move の「package は propose・check・
# ask_principal のときだけ必須」と同じ集合)。それ以外の手には package を付けずに送る。
_MOVES_WITH_PACKAGE = frozenset({"propose", "check", "ask_principal"})


class SendTurn(Protocol):
    """エージェントを呼ぶ関数の形(外から差し込む。テストではスタブ)。

    agents 側の src/agents/client.py が、先頭に base_url を足した形で持つ。呼び出し側が
    base_url を束ねてから渡す。返すのは Move の dict で、次の例外を投げる約束。
    - ConnectionError: 一時的な通信エラー・受信口が返した一時的なエラー(再試行する)
    - TimeoutError: 時間切れ(再試行する)
    - ValueError: 受信口が拒否した、または一時的と印のない失敗(LLM の出力が JSON でないなど。送り直しても直らないので、
      再試行せず schema_invalid にする。台帳 L9-5)
    """

    async def __call__(
        self,
        role: AgentRole,
        turn_input: TurnInput | AttackerTurnInput,
        *,
        nid: str,
        timeout_s: float,
    ) -> dict: ...


class FictionalAnswerer(Protocol):
    """架空人物(デモの候補者・フィクスチャの求人)の途中確認に、自動で答える口(§4.4)。

    答え方の中身(テンプレートの生の条件から答える)はフィクスチャで決まるので、1d-1 では
    差し込み口だけを作る。本物の依頼者の途中確認には使わない(画面の API が金庫へ送る。1d 以降)。
    """

    async def __call__(self, *, nid: str, side: Side, package: Package) -> PrincipalAnswerKind: ...


@dataclass(frozen=True)
class NegotiationContext:
    """レフェリーが交渉について知っておく性質。金庫の一覧(見回り)から取れるものだけ。"""

    nid: str
    mode: NegotiationMode
    candidate_principal_id: str | None  # 候補者が架空人物なら None

    def is_fictional(self, side: Side) -> bool:
        """side が架空人物か。求人側は、ハッカソンではいつもフィクスチャ(§3.7 の最終行)。"""
        if side == "employer":
            return True
        return self.candidate_principal_id is None

    def agent_role(self, side: Side) -> AgentRole:
        """呼ぶエージェントの種類。求人側は、攻撃モードの交渉のときだけ attacker(§4.1・§4.3)。"""
        if side == "candidate":
            return "candidate"
        return "attacker" if self.mode == "attack" else "employer"


@dataclass(frozen=True)
class RefereeDeps:
    """レフェリーが使う外部の部品(すべて差し込める)。"""

    vault: VaultClient
    send_turn: SendTurn
    clock: Clock = field(default_factory=SystemClock)
    sleep: Sleep = asyncio.sleep
    config: RefereeConfig = DEFAULT_WEB_CONFIG.referee
    # 架空人物の途中確認に自動で答える口。None なら、架空人物の途中確認も回答が届くまで待つ。
    answerer: FictionalAnswerer | None = None
    # 攻撃モードの求人エージェントへ毎手番渡す指示文(§8.2)を、交渉 ID から引く口。None なら空文字。
    # 指示の受け付け(攻撃画面・入口ごとのレート制限)は ③ の範囲で、ここは差し込み口だけ。
    attacker_instruction: Callable[[str], str] | None = None
    # 依頼者ごとのロック(台帳 I-4)。渡すと、本物の候補者の交渉の金庫への操作を 1 回ごとにロックの下で行う。
    # 本人の削除・利用記録の更新・段の状態の作成と、同じ依頼者の操作を 1 つずつ順に処理するため。
    locks: PrincipalLocks | None = None


class StepOutcome(enum.Enum):
    """step() が 1 回で何をしたか。"""

    MOVED = "moved"  # 手を金庫に登録した(有効な手も、無効手も含む)
    ANSWERED = "answered"  # 架空人物の途中確認に自動で答えた
    WAITING = "waiting"  # 動けない(一時停止中・本物の依頼者の回答待ち・一時的な失敗)。待って読み直す
    RETRY = "retry"  # 409(状態が変わっていた)。待たずに読み直してよいが、run() は少し待つ
    FINISHED = "finished"  # 交渉が終わった(judged)か、交渉が消えた。タスクを終える


class Referee:
    """1 つの交渉を進める。step() が 1 手ぶん、run() が終わるまでの繰り返し。"""

    def __init__(self, context: NegotiationContext, deps: RefereeDeps) -> None:
        self._context = context
        self._deps = deps
        # run() が、WAITING・RETRY の後に待つ秒数。step() が、その結果ごとに決める(台帳 L10-1)。
        self._wait_seconds = deps.config.wait_poll_interval_seconds
        # 金庫への操作の口。本物の候補者の交渉なら、依頼者のロックの下で 1 回ずつ行う口にする(台帳 I-4)。
        self._vault: VaultClient | PrincipalScopedVault = deps.vault
        if deps.locks is not None and context.candidate_principal_id is not None:
            self._vault = PrincipalScopedVault(deps.vault, deps.locks, context.candidate_principal_id)

    # ------------------------------------------------------------------
    # 実行ループ
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """交渉が終わる(judged)まで step() を繰り返す。待つ必要のあるときは sleep で間を空ける。"""
        while True:
            outcome = await self.step()
            if outcome is StepOutcome.FINISHED:
                return
            if outcome in (StepOutcome.WAITING, StepOutcome.RETRY):
                await self._deps.sleep(self._wait_seconds)

    async def step(self) -> StepOutcome:
        """金庫の状態を読み直して、手番の側について 1 手だけ進める(§4.1 の 1〜5)。"""
        self._wait_seconds = self._deps.config.wait_poll_interval_seconds
        try:
            return await self._step()
        except VaultNotFoundError:
            return StepOutcome.FINISHED  # 依頼者の削除などで、交渉そのものが消えた
        except VaultConflictError:
            return StepOutcome.RETRY  # 状態が変わった。読み直してから進める(§4.1 の 3)
        except VaultUnavailableError:
            return StepOutcome.WAITING  # 金庫が一時的に応えない。待って読み直す
        except VaultClientError as exc:
            # 404・409・503 以外(500・502・422 など)。タスクを落とすと、見回りが作り直すまで(最長 60 秒)交渉が止まる。
            # 落とさずに、待って読み直す(台帳 L9-1。金庫の読み取りの中で出た Aborted が 500 になり得る)。
            _log.error("vault call failed status=%s", exc.status_code)
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                # 404・409 は上で扱った。それ以外の 4xx(422 など)は、送り直しても直らない。読み直しのたびにエージェント(LLM)を
                # 呼ぶので、2 秒ごとに呼び直さず、見回りの間隔まで待つ(台帳 L10-1)。5xx は、これまでどおり(一時的かもしれない)。
                self._wait_seconds = self._deps.config.client_error_wait_seconds
            return StepOutcome.WAITING

    async def _step(self) -> StepOutcome:
        view, side = await self._read_turn_view()
        if view.status == "judged":
            return StepOutcome.FINISHED
        if view.to_move != side:
            return StepOutcome.RETRY  # 2 回の読み出しの間に手番が動いた。エージェントは呼ばない
        if view.paused:
            return StepOutcome.WAITING  # 一時停止中は待つ。再開は金庫の control から届く
        if view.status == "awaiting_principal":
            return await self._answer_for_fictional_principal(side, view)
        return await self._take_turn(side, view)

    async def _read_turn_view(self) -> tuple[NegotiationViewResponse, Side]:
        """手番の側の view を読む。手番は view(どちらの側でも同じ)にあるので、まず候補者側を読む。"""
        nid = self._context.nid
        first = await self._vault.get_view(nid, "candidate")
        if first.status == "judged" or first.to_move == "candidate":
            return first, "candidate"
        return await self._vault.get_view(nid, "employer"), "employer"

    # ------------------------------------------------------------------
    # 途中確認(awaiting_principal)
    # ------------------------------------------------------------------

    async def _answer_for_fictional_principal(self, side: Side, view: NegotiationViewResponse) -> StepOutcome:
        """架空人物の途中確認には自動で答える(§4.4)。本物の依頼者なら、回答が届くまで待つ。"""
        package = view.awaiting_principal_package
        answerer = self._deps.answerer
        if package is None or answerer is None or not self._context.is_fictional(side):
            return StepOutcome.WAITING
        answer = await answerer(nid=self._context.nid, side=side, package=package)
        await self._vault.post_principal_answer(
            self._context.nid,
            PrincipalAnswerRequest(expected_version=view.version, side=side, package=package, answer=answer),
        )
        return StepOutcome.ANSWERED

    # ------------------------------------------------------------------
    # 1 手ごとの流れ(§4.1 の 1〜4)
    # ------------------------------------------------------------------

    async def _take_turn(self, side: Side, view: NegotiationViewResponse) -> StepOutcome:
        nid = self._context.nid
        if view.budget.remaining_moves == 0:
            # 手番が回ってきた側の残りの手数が 0。エージェント(LLM)を呼んでも、金庫は手を受け付けずに終了処理
            # (stopped_budget)を行う(手を処理する前の停止の判定。§3.5)ので、呼び出しがむだになる(最長 60 秒)。
            # 呼ばずに、金庫に手(end)を登録して、停止の判定を効かせる(台帳 L9-3)。
            request = MoveRequest(expected_version=view.version, side=side, move="end")
        else:
            events = await self._vault.get_events(nid, side)
            turn_input: TurnInput | AttackerTurnInput = build_turn_input(side=side, view=view, events=events)
            role = self._context.agent_role(side)
            if role == "attacker":
                source = self._deps.attacker_instruction
                turn_input = to_attacker_turn_input(turn_input, source(nid) if source is not None else "")
            request = await self._ask_agent(role, turn_input, side=side, version=view.version)

        response = await self._vault.post_move(nid, request)
        return StepOutcome.FINISHED if response.status == "judged" else StepOutcome.MOVED

    async def _ask_agent(
        self, role: AgentRole, turn_input: TurnInput | AttackerTurnInput, *, side: Side, version: int
    ) -> MoveRequest:
        """エージェントを呼び、金庫に登録するリクエストにする。失敗は無効手(invalid)の登録にする。"""
        try:
            raw = await self._call_agent(role, turn_input)
            move = Move.model_validate(raw)
        except ValueError:
            # 受信口が拒否した・一時的と印のない失敗を返した(どちらも ValueError。台帳 L9-5)と、返ってきた dict が
            # Move のスキーマに合わない(pydantic の ValidationError は ValueError の一種)を、どれも schema_invalid にする。
            return self._invalid_request(side, version, "schema_invalid")
        except (ConnectionError, TimeoutError):
            return self._invalid_request(side, version, "agent_timeout")  # 再試行を使い切った
        except Exception as exc:
            # 約束にない例外。この手番を無効手にして、3 回続けば金庫が交渉を止める(§3.5)。
            # タスクごと落とすと、見回りが作り直すたびに同じ失敗を繰り返すため。
            _log.error("agent call failed side=%s error=%s", side, type(exc).__name__)
            return self._invalid_request(side, version, "agent_timeout")

        package = move.package if move.move in _MOVES_WITH_PACKAGE else None
        return MoveRequest(expected_version=version, side=side, move=move.move, package=package)

    def _invalid_request(self, side: Side, version: int, reason: RegisteredInvalidReason) -> MoveRequest:
        _log.info("registering invalid move side=%s reason=%s", side, reason)
        return MoveRequest(expected_version=version, side=side, move="invalid", reason=reason)

    async def _call_agent(self, role: AgentRole, turn_input: TurnInput | AttackerTurnInput) -> dict:
        """エージェントを 1 回呼ぶ。上限(暫定 60 秒)は、再試行の待ち時間を含む(§4.1)。

        一時的なエラー(ConnectionError)と時間切れ(TimeoutError)は、待ち時間を空けて最大
        agent_max_retries 回まで再試行する。ValueError(受信口が拒否した・一時的でない失敗)は再試行しない。
        再試行を使い切る、または上限の時間が尽きたら、最後の例外をそのまま投げる。
        """
        config = self._deps.config
        clock = self._deps.clock
        started = clock.now()

        def elapsed() -> float:
            return (clock.now() - started).total_seconds()

        retries = 0
        while True:
            remaining = config.agent_call_timeout_seconds - elapsed()
            if remaining <= 0:
                raise TimeoutError("agent call budget exhausted")
            try:
                # 上限は、差し込まれた関数に timeout_s として渡すだけでなく、ここでも強制する
                # (応答しないエージェントで、タスクが固まったままにならないように)。
                return await asyncio.wait_for(
                    self._deps.send_turn(role, turn_input, nid=self._context.nid, timeout_s=remaining),
                    timeout=remaining,
                )
            except (ConnectionError, TimeoutError):
                if retries >= config.agent_max_retries:
                    raise
                backoff = config.agent_retry_backoff_seconds
                delay = backoff[min(retries, len(backoff) - 1)]
                if elapsed() + delay >= config.agent_call_timeout_seconds:
                    raise  # 待つと上限を超える。あきらめる
                retries += 1
                await self._deps.sleep(delay)


class RefereeManager:
    """レフェリーのタスクの管理。交渉ごとに、動いているタスクを 1 つだけ持つ(§4.1)。

    web は 1 インスタンスなので、この管理はプロセスの中だけで足りる(§1.1)。仮に重なっても、
    金庫の expected_version で二重適用は起きない。
    """

    def __init__(self, deps: RefereeDeps) -> None:
        self._deps = deps
        self._tasks: dict[str, asyncio.Task] = {}

    def is_running(self, nid: str) -> bool:
        task = self._tasks.get(nid)
        return task is not None and not task.done()

    def task(self, nid: str) -> asyncio.Task | None:
        """nid のタスク(なければ None)。終わったタスクも、次に start するまでは返す。"""
        return self._tasks.get(nid)

    def start(self, context: NegotiationContext) -> bool:
        """動いているタスクがなければ作って動かす。作ったら True(冪等)。"""
        if self.is_running(context.nid):
            return False
        # 終わったタスクは覚えておかない(長く動くプロセスで、交渉の数だけ増え続けないように)。
        self._tasks = {nid: task for nid, task in self._tasks.items() if not task.done()}
        # タスクの名前に、交渉 ID を入れない(例外のときの記録に名前が出る。台帳 X-40)。
        task = asyncio.create_task(Referee(context, self._deps).run(), name="referee")
        task.add_done_callback(_log_task_end)
        self._tasks[context.nid] = task
        return True

    async def stop_all(self) -> None:
        """動いているタスクをすべて止める(停止の後始末。テストでは web が落ちた状態の再現にも使う)。"""
        running = [task for task in self._tasks.values() if not task.done()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


def _log_task_end(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()  # 取り出しておく(「取り出されなかった例外」の警告を避ける)
    if exc is not None:
        _log.error("referee task ended with an error error=%s", type(exc).__name__)
