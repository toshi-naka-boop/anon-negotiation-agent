"""台本の攻撃者と候補者(LLM を使わない)。推定区間メーターの二分探索の実演(FR-45。design.md §8.3)が使う。

レフェリー(web.referee)の send_turn の差し込み口に入れる。Gemini と同じく TurnInput だけを見て、計画(phase=plan)なら Plan、
決定(phase=decide)なら Move の dict を、使用量と一緒に返す(web.referee.SendTurn)。LLM は呼ばず、通信もしない。

- ScriptedAttacker: 攻撃モードの求人エージェント(role=attacker)の代わり。探索線(年収以外の軸を固定した線)の上で、年収を二分探索する。
  金庫の答えをすべて見られる、最悪の場合の攻撃者を表す。TurnInput だけでは候補者側の金庫の答えが分からないので、候補者側のイベントを読む口
  (read_candidate_events)から答えを受け取る。区間が 1 マスになったら、これ以上は詰められないので、交渉を終える。
  (tests/scripted_negotiators.py にあったものを、ここへ移した。テストも、ここを import する。)
- candidate_move: 攻撃の相手の候補者(role=candidate)の代わり。最初に、決まった提案を 1 つ出す。攻撃者の提案は、候補者側の金庫の評価が
  「受けられる」なら受け、そうでなければ断る。受けると交渉が終わるので、攻撃者は次の交渉で続ける(交渉をまたいで区間を積み上げる。§8.3)。
  候補者の手は、攻撃者の区間の計算に使われない(受けなかったことは「受けられない」を意味しないため。§8.3)。
- ScriptedBisectionAgents: 上の 2 つを role で振り分ける SendTurn。

探索線の値は fixtures/case3.toml のコメントと同じ(ケース 3 の候補者は、この線の上で、受ける境目と受けない境目が隣り合うマスにある)。
"""

from collections.abc import Awaitable, Callable, Sequence

from negotiation_core import AXES, Move, Package, Plan, TurnInput, Usage, Verdict
from negotiation_core.estimate_interval import Interval, estimate_interval

from vault.api_models import EventViewItem

# 探索線: 攻撃者が固定する、年収以外の軸の値(fixtures/case3.toml のコメントの探索線と同じ)。年収だけを変えた提案を出す。
SEARCH_LINE: dict = dict(
    remote_days=1, night_duty=0, review_months=6, training="available", side_job="allowed", start="within_3_months"
)
SALARY_GRID: tuple[int, ...] = AXES["salary"].grid

# 候補者の最初の提案(攻撃の相手 demo-candidate-1 = ケース 3 の候補者が受けられる組み合わせ)。攻撃者は、これに答えずに、探索線の上で提案する。
CANDIDATE_OPENING = Package(
    salary=900, remote_days=3, night_duty=0, review_months=6, training="available", side_job="allowed", start="within_3_months"
)

# 台本は LLM を呼ばないので、使用量は 0(requests だけは、1 以上という型の決まり)。ログに、実際にはないトークン数を残さない。
SCRIPTED_USAGE = Usage(
    model="scripted", prompt_tokens=0, cached_tokens=0, thoughts_tokens=0, output_tokens=0, requests=1
)

# 交渉 ID から、候補者側の見え方のイベントを読む口。最悪の場合の攻撃者が、金庫の答え(候補者の受け手としての評価)を見るために使う。
ReadEvents = Callable[[str], Awaitable[Sequence[EventViewItem]]]


def probe_package(salary: int) -> Package:
    """探索線の上の、年収が salary の提案。"""
    return Package(salary=salary, **SEARCH_LINE)


def is_on_search_line(package: Package) -> bool:
    return all(getattr(package, axis) == value for axis, value in SEARCH_LINE.items())


def next_probe_salary(interval: Interval) -> int | None:
    """区間の真ん中のグリッド点(二分探索で次に提案する年収)。区間が 1 マスなら None(グリッド上に、これ以上詰める点がない)。"""
    low = -1 if interval.lower is None else SALARY_GRID.index(interval.lower)
    high = len(SALARY_GRID) if interval.upper is None else SALARY_GRID.index(interval.upper)
    return SALARY_GRID[(low + high) // 2] if high - low > 1 else None


class ScriptedAttacker:
    """攻撃者の求人エージェントの台本(攻撃モードの role=attacker の代わり。年収の二分探索の実演。FR-45)。

    最悪の場合の攻撃者: 金庫の答えをすべて見られるものとして動く。read_candidate_events が返す候補者側のイベントから、探索線の上の
    提案への 3 値評価を集め、estimate_interval で区間を作り、真ん中の年収を提案する。区間が 1 マスになったら、これ以上は詰められない
    ので、交渉を終える。候補者が実際に受けたかどうか(LLM の手)は使わない(§8.3)。
    候補者が受けて交渉が終わる場合は、交渉をまたいで続ける。read_candidate_events が、それまでの交渉のイベントも合わせて返せばよい
    (web のメーターが、交渉 ID の一覧から区間を積み上げるのと同じ)。
    """

    def __init__(self, read_candidate_events: ReadEvents) -> None:
        self._read_candidate_events = read_candidate_events

    async def next_move(self, nid: str) -> tuple[str, Package | None]:
        """次の手(手の種類, 組み合わせ)。"""
        events = await self._read_candidate_events(nid)
        observations = [
            (event.package.salary, event.own_evaluation)
            for event in events
            if event.kind == "offer_received" and is_on_search_line(event.package)
        ]
        salary = next_probe_salary(estimate_interval(observations))
        return ("end", None) if salary is None else ("propose", probe_package(salary))


def candidate_move(turn_input: TurnInput) -> tuple[str, Package | None]:
    """候補者の台本の次の手(手の種類, 組み合わせ)。TurnInput の pending_offer(攻撃者の提案と、候補者側の金庫の評価)だけを見る。"""
    pending = turn_input.pending_offer
    if pending is None:
        return "propose", CANDIDATE_OPENING
    if pending.own_evaluation is Verdict.ACCEPTABLE:
        return "accept", None
    return "reject", None


def plan_dict(move: str, package: Package | None) -> dict:
    """計画の呼び出しで返す Plan の dict(確かめなしで、この手をそのまま出す計画)。"""
    return Plan(schema="plan/v1", checks=[], move=move, package=package).model_dump(mode="json", by_alias=True)


def move_dict(move: str, package: Package | None) -> dict:
    """決定の呼び出しで返す Move の dict。"""
    return Move(schema="move/v1", move=move, package=package).model_dump(mode="json", by_alias=True)


class ScriptedBisectionAgents:
    """SendTurn(web.referee)の形の台本。攻撃モードの交渉の、候補者(role=candidate)と攻撃者(role=attacker)。

    LLM を呼ばず、通信もしない。TurnInput だけを見て、計画(phase=plan)なら Plan、決定(phase=decide)なら Move の dict を返す。
    """

    def __init__(self, attacker: ScriptedAttacker) -> None:
        self._attacker = attacker

    async def __call__(self, role, turn_input, *, nid, timeout_s) -> tuple[dict, Usage]:
        if role == "attacker":
            move, package = await self._attacker.next_move(nid)
        elif role == "candidate":
            move, package = candidate_move(turn_input)
        else:
            raise ValueError(f"an attack negotiation has no {role!r} agent")
        payload = plan_dict(move, package) if turn_input.phase == "plan" else move_dict(move, package)
        return payload, SCRIPTED_USAGE
