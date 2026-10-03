"""レフェリー(design.md §4.1)。交渉ごとに 1 つの asyncio のタスクとして動き、LLM の呼び出しを担う。

秘密に触れる判断・状態の遷移・記録は、すべて金庫に任せる。レフェリーがするのは次だけ(1 手ごとの流れ。§4.1 の 1〜7。LLM の
呼び出しは 1 手番に最大 2 回)。
- 手番の側の残りの手数が 0 なら、LLM を呼ばずに end を登録して、金庫の停止の判定を効かせる(台帳 L9-3)。
- 計画: 金庫の view とイベント列から TurnInput(phase=plan)を組み立ててエージェントを呼び、2 段で読んで Plan にする
  (negotiation_core.parse_plan。§2.7・§4.1 の 2。台帳 X-74・X-79: PlanEnvelope で schema と checks だけを検証し、checks があれば
  move・package は型検証せずに捨てる。空なら move・package を Move の規則で検証する)。checks が空なら、その手をそのまま登録する。
  checks と move の両方があれば、checks を実行して move を無視する(台帳 C-51。move の中身が check・グリッド外でも無効にしない。L16-1)。
- 確かめ: checks を並びの順に 1 つずつ処理する。その側の見え方にすでにある評価は、金庫を呼ばずに埋める(web.turn_input.
  known_evaluations)。残りの評価回数が「残りの手数 ＋ 残りの途中確認数」以下なら、確かめずに null にする(台帳 C-47・X-48)。
  それ以外は、expected_version を付けて金庫の check を登録し、view を読み直して評価と version を取る。「受けられる」が出たら、
  残りは確かめない。409 なら、この手番をやめて状態を読み直す。金庫の check を登録できるのは、レフェリーだけ(エージェントは出せない)。
- 決定: view とイベント列を読み直して(確かめで残りの評価回数と last_check・history が変わっている。台帳 L12-2)、TurnInput
  (phase=decide。checked に確かめの結果)を組み立てて呼び、Move として検証して登録する。計画が無効手になった手番では、決定を呼ばない。
- 検証に失敗したとき・エージェントが応答しないとき・出力が切れたときは、move=invalid(schema_invalid・agent_timeout・
  output_truncated)として登録する。途中確認(awaiting_principal)・一時停止中は、金庫の状態が変わるまで待つ。

LLM に送る前に、物理の呼び出し数(交渉ごと・1 日)を `(default)` のトランザクションで数える(RefereeDeps.llm_budget。web.llm_budget。
再試行も 1 回と数え、429・5xx でも戻さない)。上限に達していたら、送らずに、金庫の control{stop_cost_limit} で「なし」にして、ログに
残す(値は含まない。台帳 X-52)。カウンタに書けないときも送らず、金庫が応えないときと同じく待って読み直す(閉じる側。台帳 X-50)。

時刻と待ち時間は注入できる(clock・sleep)ので、テストは sleep せずに 1 手ずつ進められる
(step())。実際のタスクの起動・作り直しは RefereeManager と見回り(web.sweeper)が行う。

本物の候補者の交渉では、金庫への操作を 1 回ごとに、その依頼者のロックの下で行う(台帳 I-4。
RefereeDeps.locks を渡したとき。エージェントを呼んでいる間はロックを持たない)。

待って読み直す間隔は、既定では wait_poll_interval_seconds(暫定 2 秒)。ただし金庫が、送り直しても直らないエラー(404・409
以外の 4xx。422 など)を返した後は、見回りの間隔(暫定 60 秒。client_error_wait_seconds)にする(台帳 L10-1)。読み直すたびに
エージェント(LLM)を呼ぶので、直らないエラーで 2 秒ごとに呼び直すと、手番の期限で終わるまでの間に、LLM の呼び出しを使い果たす。

ログには、側・呼び出しの種類・手の種類・理由(列挙値)・例外の型名・金庫の HTTP ステータス・使用量の数(トークン数など)だけを
書く。交渉 ID・依頼者 ID・組み合わせの値は書かない(§3.8。台帳 X-40: ログは本人の削除の後も残り、ID から交渉の時刻・失敗の理由・側を
たどれてしまう)。例外の型名だけを書くのは、検証エラーのメッセージに入力値が含まれるため。タスクの名前にも、ID を入れない。
"""

import asyncio
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from negotiation_core import AttackerTurnInput, CheckedPackage, Move, Package, Plan, Side, TurnInput, Usage, Verdict, parse_plan

from vault.api_models import ControlRequest, EventViewItem, MoveRequest, NegotiationViewResponse, PrincipalAnswerRequest
from vault.clock import Clock, SystemClock
from vault.models import NegotiationMode, PrincipalAnswerKind, RegisteredInvalidReason

from web.config import DEFAULT_WEB_CONFIG, RefereeConfig
from web.llm_budget import LlmBudget, LlmBudgetUnavailable
from web.locks import PrincipalLocks, PrincipalScopedVault
from web.turn_input import build_turn_input, known_evaluations, package_key, to_attacker_turn_input
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

# package を伴って金庫へ登録する、エージェントの手(negotiation_core.Move の「package は propose・ask_principal のときだけ
# 必須」と同じ集合)。それ以外の手には package を付けずに送る。金庫の check(package を伴う)は、レフェリーの確かめで、別の経路。
_MOVES_WITH_PACKAGE = frozenset({"propose", "ask_principal"})


class SendTurn(Protocol):
    """エージェントを呼ぶ関数の形(外から差し込む。テストではスタブ)。

    agents 側の src/agents/client.py が、先頭に base_url を足した形で持つ。呼び出し側が
    base_url を束ねてから渡す。返すのは (payload, usage): payload は、turn_input.phase が plan なら Plan、decide なら Move の
    dict、usage はその呼び出しの使用量(negotiation_core.Usage)。次の例外を投げる約束。
    - ConnectionError: 一時的な通信エラー・受信口が返した一時的なエラー(再試行する)
    - TimeoutError: 時間切れ(再試行する)
    - ValueError: 受信口が拒否した、または一時的と印のない失敗(LLM の出力が JSON でない・応答の封筒の検証の失敗など。
      送り直しても直らないので、再試行せず schema_invalid にする。台帳 L9-5・X-58)
    - TruncatedOutputError(ValueError): 出力が max_output_tokens で切れた(属性 usage: Usage | None)。再試行せず
      output_truncated にする(台帳 C-53)
    """

    async def __call__(
        self,
        role: AgentRole,
        turn_input: TurnInput | AttackerTurnInput,
        *,
        nid: str,
        timeout_s: float,
    ) -> tuple[dict, Usage]: ...


def is_truncated_output(exc: BaseException) -> bool:
    """出力が max_output_tokens で切れたことを示す例外(agents.client.TruncatedOutputError。ValueError の一種)か。

    レフェリーは agents を import しない(差し込み口の向こう側。agents を束ねるのは web.app だけ)ので、型の名前で見分ける。
    """
    return any(cls.__name__ == "TruncatedOutputError" for cls in type(exc).__mro__)


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
    # 指示は web のメモリにだけ持つ(web.attack.memory。台帳 P-17)。この web が持っていない交渉(再起動で消えた)には None を返すと、
    # 攻撃者の手番で、その交渉を取消(「なし」)にして終える。
    attacker_instruction: Callable[[str], str | None] | None = None
    # デモ・攻撃(live 以外)の交渉の、LLM に渡す入力を作るたびに呼ぶ口(壁 2。§8.1。web.attack.memory.LlmContextRecorder.record)。
    # 本物の利用者の交渉(live)では呼ばない。
    turn_recorder: Callable[[NegotiationContext, Side, TurnInput | AttackerTurnInput], None] | None = None
    # 依頼者ごとのロック(台帳 I-4)。渡すと、本物の候補者の交渉の金庫への操作を 1 回ごとにロックの下で行う。
    # 本人の削除・利用記録の更新・段の状態の作成と、同じ依頼者の操作を 1 つずつ順に処理するため。
    locks: PrincipalLocks | None = None
    # LLM に送る前の、物理の呼び出し数の計上(交渉ごと・1 日。§8.2)。本番の組み立て(web.services)は必ず渡す。
    # None にできるのは、count_llm_calls=False を明示したレフェリー単体のテストだけ(台帳 X-60: 省略して起動できないように)。
    llm_budget: LlmBudget | None = None
    count_llm_calls: bool = True
    # 1 回の計画から実行する確かめの数の上限(§2.7。[web.llm_budget] max_checks_per_plan)
    max_checks_per_plan: int = DEFAULT_WEB_CONFIG.llm_budget.max_checks_per_plan


class StepOutcome(enum.Enum):
    """step() が 1 回で何をしたか。"""

    MOVED = "moved"  # 手(または無効手)を金庫に登録した。確かめだけで手番を終えることはない(決定まで進む)
    ANSWERED = "answered"  # 架空人物の途中確認に自動で答えた
    WAITING = "waiting"  # 動けない(一時停止中・本物の依頼者の回答待ち・一時的な失敗)。待って読み直す
    RETRY = "retry"  # 409(状態が変わっていた)。待たずに読み直してよいが、run() は少し待つ
    FINISHED = "finished"  # 交渉が終わった(judged)か、交渉が消えた。タスクを終える


class _TurnEnded(Exception):
    """この手番をやめる(確かめの途中で、状態が変わった・交渉が終わった)。outcome が step() の結果。"""

    def __init__(self, outcome: StepOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


class _AttackContextLost(Exception):
    """攻撃モードの交渉の文脈(攻撃の指示)を、この web が持っていない(再起動で消えた。台帳 P-17)。交渉を取消にして終える。"""


class _CostLimitReached(Exception):
    """物理の呼び出し数の上限(交渉ごと・1 日)に達していて、送れない。limit は、どちらの上限か。"""

    def __init__(self, limit: Literal["daily", "negotiation"]) -> None:
        super().__init__(limit)
        self.limit = limit


@dataclass(frozen=True)
class _InvalidOutput:
    """エージェントの呼び出しの結果が、手(Plan・Move)にならなかった。reason の無効手として登録する。"""

    reason: RegisteredInvalidReason


class Referee:
    """1 つの交渉を進める。step() が 1 手番ぶん(計画・確かめ・決定)、run() が終わるまでの繰り返し。"""

    def __init__(self, context: NegotiationContext, deps: RefereeDeps) -> None:
        if deps.llm_budget is None and deps.count_llm_calls:
            # 台帳 X-60: 計上を省いたまま動かせると、費用の歯止め(§8.2)が丸ごと効かない。テストだけが明示的に外せる
            raise ValueError("RefereeDeps.llm_budget is required (set count_llm_calls=False only in tests)")
        self._context = context
        self._deps = deps
        # run() が、WAITING・RETRY の後に待つ秒数。step() が、その結果ごとに決める(台帳 L10-1)。
        self._wait_seconds = deps.config.wait_poll_interval_seconds
        # 直前に書いた「金庫が応えない」理由(ステータス・通信エラーの型名)。同じ理由が続く間は書き直さない(台帳 I-7)。
        self._unavailable_cause: tuple[int | None, str] | None = None
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
                await self._log_call_count()
                return
            if outcome in (StepOutcome.WAITING, StepOutcome.RETRY):
                await self._deps.sleep(self._wait_seconds)

    async def step(self) -> StepOutcome:
        """金庫の状態を読み直して、手番の側について 1 手番だけ進める(§4.1 の 1〜5。確かめを含む)。"""
        self._wait_seconds = self._deps.config.wait_poll_interval_seconds
        try:
            outcome = await self._step()
        except VaultNotFoundError:
            return StepOutcome.FINISHED  # 依頼者の削除などで、交渉そのものが消えた
        except VaultConflictError:
            return StepOutcome.RETRY  # 状態が変わった。読み直してから進める(§4.1 の 3)
        except VaultUnavailableError as exc:
            self._log_unavailable(exc)
            return StepOutcome.WAITING  # 金庫が一時的に応えない。待って読み直す
        except LlmBudgetUnavailable:
            return StepOutcome.WAITING  # 物理の呼び出し数のカウンタに書けない。送らずに、待って読み直す(閉じる側。台帳 X-50)
        except VaultClientError as exc:
            # 404・409・503 以外(500・502・422 など)。タスクを落とすと、見回りが作り直すまで(最長 60 秒)交渉が止まる。
            # 落とさずに、待って読み直す(台帳 L9-1。金庫の読み取りの中で出た Aborted が 500 になり得る)。
            _log.error("vault call failed status=%s", exc.status_code)
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                # 404・409 は上で扱った。それ以外の 4xx(422 など)は、送り直しても直らない。読み直しのたびにエージェント(LLM)を
                # 呼ぶので、2 秒ごとに呼び直さず、見回りの間隔まで待つ(台帳 L10-1)。5xx は、これまでどおり(一時的かもしれない)。
                self._wait_seconds = self._deps.config.client_error_wait_seconds
            return StepOutcome.WAITING
        self._unavailable_cause = None
        return outcome

    def _log_unavailable(self, exc: VaultUnavailableError) -> None:
        """金庫が応えない理由(ステータスと、通信エラーの型名)を、理由が変わったときに 1 回だけ書く(台帳 I-7)。

        ID トークンを取れない・宛先違いのとき(ServiceAuthError)もここに来るので、運用で気づける。待つたびに(2 秒ごとに)
        書き直さない。exc の文(金庫の detail を含み得る)と、トークンの値は書かない(台帳 X-40)。
        """
        cause = type(exc.__cause__).__name__ if exc.__cause__ is not None else "-"
        key = (exc.status_code, cause)
        if key != self._unavailable_cause:
            self._unavailable_cause = key
            _log.warning("vault unavailable status=%s cause=%s; waiting", exc.status_code, cause)

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
    # 1 手ごとの流れ(§4.1 の 1〜5)
    # ------------------------------------------------------------------

    async def _take_turn(self, side: Side, view: NegotiationViewResponse) -> StepOutcome:
        if view.budget.remaining_moves == 0:
            # 手番が回ってきた側の残りの手数が 0。エージェント(LLM)を呼んでも、金庫は手を受け付けずに終了処理
            # (stopped_budget)を行う(手を処理する前の停止の判定。§3.5)ので、呼び出しがむだになる(最長 60 秒)。
            # 呼ばずに、金庫に手(end)を登録して、停止の判定を効かせる(台帳 L9-3)。
            return await self._register(MoveRequest(expected_version=view.version, side=side, move="end"))
        try:
            return await self._play_turn(side, view)
        except _TurnEnded as ended:
            return ended.outcome
        except _CostLimitReached as reached:
            return await self._stop_for_cost_limit(reached.limit)
        except _AttackContextLost:
            return await self._cancel_without_attack_context()

    async def _play_turn(self, side: Side, view: NegotiationViewResponse) -> StepOutcome:
        """計画 → 確かめ → 決定(§4.1 の 2〜5)。エージェントの呼び出しは、最大 2 回。"""
        nid = self._context.nid
        events = await self._vault.get_events(nid, side)

        plan = await self._ask(Plan, side, view, events, checked=[])
        if isinstance(plan, _InvalidOutput):
            return await self._register(self._invalid_request(side, view.version, plan.reason))  # 決定は呼ばない
        if not plan.checks:
            # 確かめの要らない手(accept・reject・end・ask_principal、確かめ済みの案の propose など)は、そのまま登録する。
            return await self._register(self._move_request(side, view.version, plan.move, plan.package))

        # checks と move の両方があるときも、checks を実行して move を無視する(台帳 C-51)。parse_plan が move・package を捨てている。
        checked = await self._run_checks(side, view, events, plan.checks)

        # 確かめで、残りの評価回数と last_check・history が変わっている。view とイベント列を読み直して決定へ(台帳 L12-2)。
        view = await self._vault.get_view(nid, side)
        self._ensure_playable(view, side)
        events = await self._vault.get_events(nid, side)
        decision = await self._ask(Move, side, view, events, checked=checked)
        if isinstance(decision, _InvalidOutput):
            return await self._register(self._invalid_request(side, view.version, decision.reason))
        return await self._register(self._move_request(side, view.version, decision.move, decision.package))

    async def _run_checks(
        self, side: Side, view: NegotiationViewResponse, events: list[EventViewItem], planned: list[Package]
    ) -> list[CheckedPackage]:
        """計画の checks を、並びの順に 1 つずつ処理する(§4.1 の 3)。結果は、計画の順に並べる(確かめなかった案は null)。

        1. その側の見え方にすでにある評価は、金庫を呼ばずに埋める(評価を消費しない)。
        2. 残りの評価回数が「残りの手数 ＋ 残りの途中確認数」以下なら、確かめずに null にする(台帳 C-47・X-48)。
        3. それ以外は、expected_version を付けて金庫の check を登録し、view を読み直して評価と version を取る。
        4. 「受けられる」が出たら、残りは null にする(出したい順に並んでいるので、見つかれば十分)。
        409 は VaultConflictError のまま伝わり、step() が、この手番をやめて状態を読み直す(次は計画から始めるが、済んだ確かめは
        1 で履歴から埋まる)。
        """
        known = known_evaluations(view=view, events=events)
        checked: list[CheckedPackage] = []
        current = view
        done = False  # 「受けられる」が出た(または確かめを続けられなくなった)。残りは確かめない
        for index, package in enumerate(planned):
            if done or index >= self._deps.max_checks_per_plan:
                checked.append(CheckedPackage(package=package, evaluation=None))
                continue
            verdict = known.get(package_key(package))
            if verdict is None:
                budget = current.budget
                if budget.remaining_evaluations > budget.remaining_moves + budget.remaining_principal_checks:
                    verdict, current = await self._check_in_vault(side, current, package)
                    if verdict is None:
                        done = True  # 評価を取れなかった(無効になった)。確かめを続けない
                    else:
                        known[package_key(package)] = verdict
            checked.append(CheckedPackage(package=package, evaluation=verdict))
            done = done or verdict is Verdict.ACCEPTABLE
        return checked

    async def _check_in_vault(
        self, side: Side, view: NegotiationViewResponse, package: Package
    ) -> tuple[Verdict | None, NegotiationViewResponse]:
        """金庫の check を 1 回登録して(評価 1 消費)、view を読み直す。評価(取れなければ None)と、読み直した view を返す。"""
        nid = self._context.nid
        response = await self._vault.post_move(
            nid, MoveRequest(expected_version=view.version, side=side, move="check", package=package)
        )
        if response.status == "judged":
            raise _TurnEnded(StepOutcome.FINISHED)
        view = await self._vault.get_view(nid, side)
        self._ensure_playable(view, side)
        last_check = view.last_check
        if response.valid and last_check is not None and last_check.package == package:
            return last_check.own_evaluation, view
        return None, view

    def _ensure_playable(self, view: NegotiationViewResponse, side: Side) -> None:
        """手番の途中で読み直した view が、まだ side の手番で動けるか。そうでなければ、この手番をやめる(次の step() が読み直す)。"""
        if view.status == "judged":
            raise _TurnEnded(StepOutcome.FINISHED)
        if view.status != "active" or view.paused or view.to_move != side:
            raise _TurnEnded(StepOutcome.RETRY)  # 一時停止・取消・期限切れなどが割り込んだ

    async def _register(self, request: MoveRequest) -> StepOutcome:
        """金庫に手(または無効手)を登録する。409 は VaultConflictError のまま伝わる。"""
        response = await self._vault.post_move(self._context.nid, request)
        return StepOutcome.FINISHED if response.status == "judged" else StepOutcome.MOVED

    async def _stop_for_cost_limit(self, limit: Literal["daily", "negotiation"]) -> StepOutcome:
        """物理の呼び出し数の上限に達したので、送らずに、金庫の control{stop_cost_limit} で交渉を「なし」にする(台帳 X-52)。

        冪等で、どの状態(一時停止中・途中確認中・直前の操作が 409 になった後)からでも効く。ログには、どちらの上限かだけを残す。
        """
        _log.warning("llm call limit reached; stopping the negotiation limit=%s", limit)
        response = await self._vault.stop_cost_limit(self._context.nid)
        return StepOutcome.FINISHED if response.status == "judged" else StepOutcome.WAITING

    async def _cancel_without_attack_context(self) -> StepOutcome:
        """攻撃の指示を web が持っていない(再起動で消えた。台帳 P-17)ので、攻撃者を呼ばずに、交渉を取消(「なし」)にして終える。冪等。

        攻撃者の手番で気づく(候補者が先に打つので、手番の前に指示がなくなっていても、候補者の手番は進む)。
        """
        _log.warning("an attack negotiation lost its instruction; cancelling it")
        response = await self._vault.control(self._context.nid, ControlRequest(side="employer", action="cancel"))
        return StepOutcome.FINISHED if response.status == "judged" else StepOutcome.WAITING

    async def _log_call_count(self) -> None:
        """交渉の終了時に、この交渉の物理の呼び出し数をログに残す(§8.2。値は含まない数だけ)。数えられなければ、何もしない。"""
        budget = self._deps.llm_budget
        if budget is None:
            return
        try:
            count = await budget.negotiation_count(self._context.nid)
        except LlmBudgetUnavailable:
            return
        _log.info("negotiation finished llm_calls=%d", count)

    # ------------------------------------------------------------------
    # エージェントの呼び出し
    # ------------------------------------------------------------------

    async def _ask(
        self,
        model: type[Plan] | type[Move],
        side: Side,
        view: NegotiationViewResponse,
        events: list[EventViewItem],
        *,
        checked: list[CheckedPackage],
    ) -> Plan | Move | _InvalidOutput:
        """エージェントを 1 回呼び(計画なら Plan、決定なら Move)、検証する。失敗は _InvalidOutput(無効手の理由)にする。

        計画は parse_plan で 2 段に読む(Plan 型で一括には検証しない。§2.7)。決定は Move 型で strict に検証する。

        上限に達していて送れないときは _CostLimitReached、カウンタに書けないときは LlmBudgetUnavailable のまま伝える
        (無効手にしない。送っていないので、エージェントの失敗ではない)。
        """
        nid = self._context.nid
        phase = "plan" if model is Plan else "decide"
        turn_input: TurnInput | AttackerTurnInput = build_turn_input(
            side=side, view=view, events=events, phase=phase, checked=checked
        )
        role = self._context.agent_role(side)
        if role == "attacker":
            source = self._deps.attacker_instruction
            instruction = source(nid) if source is not None else ""
            if instruction is None:
                raise _AttackContextLost
            turn_input = to_attacker_turn_input(turn_input, instruction)
        recorder = self._deps.turn_recorder
        if recorder is not None and self._context.mode != "live":
            recorder(self._context, side, turn_input)
        try:
            payload, usage = await self._call_agent(role, turn_input)
            self._log_usage(side, phase, "ok", usage)
            return parse_plan(payload) if model is Plan else model.model_validate(payload)
        except (_CostLimitReached, LlmBudgetUnavailable):
            raise
        except ValueError as exc:
            # 受信口が拒否した・一時的と印のない失敗を返した(どちらも ValueError。台帳 L9-5)・応答の封筒の検証に失敗した
            # (台帳 X-58)と、返ってきた dict が Plan・Move のスキーマに合わない(pydantic の ValidationError は ValueError
            # の一種)は、schema_invalid にする。出力が max_output_tokens で切れたとき(TruncatedOutputError)だけは、
            # 次の呼び出しで短く答える手がかりになるよう、output_truncated にする(台帳 C-53)。
            if is_truncated_output(exc):
                self._log_usage(side, phase, "truncated", getattr(exc, "usage", None))
                return _InvalidOutput("output_truncated")
            return _InvalidOutput("schema_invalid")
        except (ConnectionError, TimeoutError):
            return _InvalidOutput("agent_timeout")  # 再試行を使い切った
        except Exception as exc:
            # 約束にない例外。この手番を無効手にして、3 回続けば金庫が交渉を止める(§3.5)。
            # タスクごと落とすと、見回りが作り直すたびに同じ失敗を繰り返すため。
            _log.error("agent call failed side=%s phase=%s error=%s", side, phase, type(exc).__name__)
            return _InvalidOutput("agent_timeout")

    def _log_usage(self, side: Side, phase: str, outcome: str, usage: Usage | None) -> None:
        """呼び出しごとの使用量を、ログに残す(§4.3。トークン数などの数だけ。値は含まない)。"""
        if usage is None:
            _log.info("agent call side=%s phase=%s outcome=%s usage=none", side, phase, outcome)
            return
        _log.info(
            "agent call side=%s phase=%s outcome=%s prompt_tokens=%d cached_tokens=%d thoughts_tokens=%d "
            "output_tokens=%d requests=%d",
            side,
            phase,
            outcome,
            usage.prompt_tokens,
            usage.cached_tokens,
            usage.thoughts_tokens,
            usage.output_tokens,
            usage.requests,
        )

    def _move_request(self, side: Side, version: int, move: str, package: Package | None) -> MoveRequest:
        """エージェントの手を、金庫に登録するリクエストにする。package は、それを伴う手(propose・ask_principal)だけに付ける。"""
        return MoveRequest(
            expected_version=version,
            side=side,
            move=move,
            package=package if move in _MOVES_WITH_PACKAGE else None,
        )

    def _invalid_request(self, side: Side, version: int, reason: RegisteredInvalidReason) -> MoveRequest:
        _log.info("registering invalid move side=%s reason=%s", side, reason)
        return MoveRequest(expected_version=version, side=side, move="invalid", reason=reason)

    async def _call_agent(self, role: AgentRole, turn_input: TurnInput | AttackerTurnInput) -> tuple[dict, Usage]:
        """エージェントを 1 回呼ぶ(物理の送信は、再試行を含めて、1 回ごとに数える)。上限(暫定 60 秒)は、再試行の待ち時間を含む(§4.1)。

        一時的なエラー(ConnectionError)と時間切れ(TimeoutError)は、待ち時間を空けて最大
        agent_max_retries 回まで再試行する。ValueError(受信口が拒否した・一時的でない失敗。TruncatedOutputError を含む)は
        再試行しない。再試行を使い切る、または上限の時間が尽きたら、最後の例外をそのまま投げる。
        送る前に、物理の呼び出し数を数える(_reserve_send)。429・5xx が返っても戻さない(台帳 X-56)。
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
            await self._reserve_send()
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

    async def _reserve_send(self) -> None:
        """LLM に送る前に、物理の呼び出し数(交渉ごと・1 日)を 1 つのトランザクションで数える(§4.1・§8.2)。

        上限に達していれば、送らずに _CostLimitReached。カウンタに書けなければ LlmBudgetUnavailable(送らない)。
        """
        budget = self._deps.llm_budget
        if budget is None:
            return
        reservation = await budget.reserve(self._context.nid)
        if not reservation.granted:
            assert reservation.refused_by is not None
            raise _CostLimitReached(reservation.refused_by)


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

    def running_nids(self) -> list[str]:
        """動いているタスクがある交渉の ID(進行中の交渉の一覧。作成の入場の制限が、未消化分を数える元。§8.2)。

        交渉を作ったときに start するので、作成した交渉は、見回りを待たずに、その場でこの一覧に入る。
        """
        return [nid for nid, task in self._tasks.items() if not task.done()]

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
