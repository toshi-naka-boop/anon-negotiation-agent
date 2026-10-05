"""本物の依頼者ごとの 24 時間の評価予算(design.md §3.5)。

交渉の作成時に、交渉ごとの評価上限(暫定 17)を、依頼者の 24 時間の評価予算(暫定 170)から
先に取る(予約)。固定窓: window_started_at から 24 時間たっていたら、その時点で窓を
今の時刻から張り直し、used を 0 に戻す(§3.5・§3.4 の期限の考え方と同じ「固定窓」の読み方。
交渉を作り直しても used は減らないので、AC-06 の「交渉を作り直しても依頼者ごとの予算は
回復しない」を満たす)。
"""

import datetime as dt

from vault.models import EvaluationBudgetWindow

WINDOW_SECONDS = 24 * 60 * 60


def reserve(
    existing: EvaluationBudgetWindow | None,
    now: dt.datetime,
    daily_budget: int,
    amount: int,
) -> tuple[bool, EvaluationBudgetWindow]:
    """amount を予約できるなら (True, 更新後の窓) を、足りなければ (False, 今の窓) を返す。

    足りないときに返す「今の窓」は、時間切れなら張り直した(used=0 の)ものにする
    (呼び出し側が、予約に失敗した場合でも書き込んでよい値にするため)。
    """
    if existing is None or (now - existing.window_started_at).total_seconds() >= WINDOW_SECONDS:
        window_started_at = now
        used = 0
    else:
        window_started_at = existing.window_started_at
        used = existing.used

    if used + amount > daily_budget:
        return False, EvaluationBudgetWindow(window_started_at=window_started_at, used=used)
    return True, EvaluationBudgetWindow(window_started_at=window_started_at, used=used + amount)
