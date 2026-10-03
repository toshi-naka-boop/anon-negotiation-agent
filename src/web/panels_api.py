"""並べて見る画面(FR-39)の 2 つのパネル「最悪漏れてもここまで」「まだ隠しているもの」の API(design.md §7・§5 の 7・§2.5。台帳 X-6・I-23)。画面は含まない。

| メソッド・パス | 呼べる人 | 返すもの |
|---|---|---|
| GET /v1/principals/{pid}/panels | 本人(セッションの依頼者。pid は自分の依頼者 ID か、その別名 me) | 2 つのパネル |

me は、セッションの依頼者を指す別名。依頼者 ID は HttpOnly のクッキーの中にあり、画面からは読めないので、自分の ID を知らなくても呼べるようにした。
セッションがなければ 401、ほかの依頼者の ID なら 403(本人向けの経路と同じ。§6.3)。面談を送っていない(金庫にポリシーがない)依頼者は 404。

どちらのパネルも、表示のたびに、金庫の読み出し口(GET /v1/principals/{pid}/policy と同じ。丸め済みポリシーと外した軸)から作る。
web には保存しない(読むだけで何も書かない)。生の値はどこにも保存していないので、ここに出るのは丸めた後のグリッドの値だけ。
軸ごとに 1 件、語彙(§2.1)の順。

- 「最悪漏れてもここまで」(worst_case): 金庫の答えをすべて見られた場合に知られ得る、丸めた後のマス。{axis, removed, cells}
  - removed: 外した軸(§2.4)。ポリシーにこの軸の意向は入っていないので、cells は空にする(画面は「外しています(交渉中に確認)」と出す)。
  - cells: アンカーがこの軸で条件を付けている(軸について中立でない。§2.2)値を、丸めたマスで。同じマスは 1 つにまとめ、小さい順に並べる。
    - 年収(離散軸でない数値軸): アンカーの値と、その隣のグリッド点の間 {low, high}。受けるアンカーは本人にとって良い側へ丸めた値なので、生の値は
      悪い側の隣まで、受けないアンカーは悪い側へ丸めた値なので、良い側の隣まで(§2.5。例: 「620 万以上」を 650 に寄せれば、600〜650 のマス)。
    - 離散の数値軸: その値そのもの(low == high)。離散軸は、丸めても情報が減らない(§2.1)。
    - 区分軸: ポリシーが限っている値 {value}。* は条件でないので出さない。
- 「まだ隠しているもの」(still_hidden): 値を持たない。種類とマスの数だけ。{axis, kind, cells, removed}
  - kind: 数値軸は range(正確な値が、マスの幅の中のどこか)、区分軸は category。
  - cells(マスの数): 年収は、正確な境目が隠れているマスの数(worst_case の cells の数)。外した軸は、その軸の値の数(全体が未確定)。
    それ以外の離散軸は 0(条件そのものが知られ得る。§5 の 3)。マスの範囲や値は、ここには出さない(範囲は worst_case にある)。
  - 「辞めた理由(面談時に破棄済み)」のような、軸でない項目は含めない(画面の固定の文)。

ログには何も書かない(ポリシーの値を、ここから出さない)。
"""

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from negotiation_core import AXES, AXIS_KEYS, Policy, best_value, worst_value

from vault.api_models import PolicyView

from web.services import WebServices
from web.session import PrincipalSession

# pid の別名: セッションの依頼者。
ME = "me"


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RangeCell(_ResponseModel):
    """数値軸のマス: low 以上 high 以下のグリッドの値(隣り合う 2 点。low == high なら 1 点)。"""

    low: int
    high: int


class ValueCell(_ResponseModel):
    """区分軸で、ポリシーが限っている値。"""

    value: str


class AxisWorstCase(_ResponseModel):
    axis: str
    removed: bool
    cells: list[RangeCell | ValueCell]


class AxisStillHidden(_ResponseModel):
    axis: str
    kind: Literal["range", "category"]
    removed: bool
    cells: int


class PrincipalPanels(_ResponseModel):
    worst_case: list[AxisWorstCase]
    still_hidden: list[AxisStillHidden]


def _rounded_cells(policy: Policy, axis: str) -> list[RangeCell | ValueCell]:
    """policy のアンカーが axis に付けている条件を、丸めたマスで返す(外した軸は、呼び出し側が除く)。"""
    definition, side = AXES[axis], policy.side
    if definition.kind == "categorical":
        used = {getattr(anchor, axis) for anchor in (*policy.accept_anchors, *policy.reject_anchors)} - {"*"}
        return [ValueCell(value=value) for value in definition.grid if value in used]
    grid = definition.grid
    worse = -1 if definition.direction_for(side) == "higher_is_better" else 1  # グリッドの添字で、本人にとって悪い側へ進む向き
    cells: set[tuple[int, int]] = set()
    # (アンカー, 軸について中立な値, 生の値がある側へ進む向き)。条件を付けているアンカーは、中立な値の端にないので、その向きの隣が必ずある。
    for anchors, neutral, toward in (
        (policy.accept_anchors, worst_value(axis, side), worse),
        (policy.reject_anchors, best_value(axis, side), -worse),
    ):
        for anchor in anchors:
            value = getattr(anchor, axis)
            if value == neutral:
                continue
            index = grid.index(value)
            neighbour = grid[index] if definition.discrete else grid[index + toward]
            cells.add((min(grid[index], neighbour), max(grid[index], neighbour)))
    return [RangeCell(low=low, high=high) for low, high in sorted(cells)]


def build_panels(view: PolicyView) -> PrincipalPanels:
    """金庫の丸め済みポリシー(と外した軸)から、2 つのパネルを作る。純粋な計算で、何も読まず、何も書かない。"""
    removed_axes = set(view.removed_axes)
    worst_case: list[AxisWorstCase] = []
    still_hidden: list[AxisStillHidden] = []
    for axis in AXIS_KEYS:
        definition = AXES[axis]
        removed = axis in removed_axes
        cells = [] if removed else _rounded_cells(view.policy, axis)
        worst_case.append(AxisWorstCase(axis=axis, removed=removed, cells=cells))
        if removed:
            hidden = len(definition.grid)
        else:
            hidden = 0 if definition.discrete else len(cells)
        still_hidden.append(
            AxisStillHidden(
                axis=axis, kind="range" if definition.kind == "numeric" else "category", removed=removed, cells=hidden
            )
        )
    return PrincipalPanels(worst_case=worst_case, still_hidden=still_hidden)


def build_panels_router(services: WebServices, require_session: Callable[..., PrincipalSession]) -> APIRouter:
    """2 つのパネルのルートを作る。api.py の build_router が include する。

    require_session は api.py の依存(セッションがなければ 401 にして、依頼者の情報を返す)。
    """
    router = APIRouter()

    @router.get("/v1/principals/{pid}/panels", response_model=PrincipalPanels)
    async def principal_panels(pid: str, session: PrincipalSession = Depends(require_session)) -> PrincipalPanels:
        """並べて見る画面の 2 つのパネル(FR-39)。本人の丸め済みポリシーを金庫から読んで作る。"""
        if pid not in (ME, session.principal_id):
            raise HTTPException(status_code=403, detail="forbidden")
        return build_panels(await services.vault.get_policy(session.principal_id))

    return router
