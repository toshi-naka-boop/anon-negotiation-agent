"""停止の判定(design.md §3.5 FR-12、AC-06)。

回数・手数・連続無効手だけを入力に取り、ポリシーを引数に取らない。そのため、無効かどうかは
金庫の応答(自分側の評価を含む)だけで決まり、同じ「手と金庫の応答」の並びなら、ポリシーを
差し替えても同じ手番で止まる(AC-06)。

同じ関数を 2 箇所から呼ぶ: (1) 手番が来た側について、手を処理する前(手数の上限だけが
効き得る。連続無効手は前回までにすでに判定済みのはず)。(2) 無効手を記録した直後、その側の
更新後の counters について(手数・連続無効手のどちらでも止まり得る)。
"""

from vault.models import EndReason


def determine_stop_reason(
    *, moves_used: int, consecutive_invalid: int, moves_budget: int, consecutive_invalid_limit: int
) -> EndReason | None:
    """止めるべきなら理由を、続けてよければ None を返す。

    手数の上限を先に見る(§3.5: 「手番が回ってきた側の残りの手数が 0 なら、手を受け付けずに
    終了処理を行う」)。次に連続無効手の上限を見る。
    """
    if moves_used >= moves_budget:
        return "stopped_budget"
    if consecutive_invalid >= consecutive_invalid_limit:
        return "stopped_invalid"
    return None
