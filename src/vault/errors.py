"""金庫の例外階層(design.md §3.3・§3.5)。

app.py がこれらを HTTP ステータスに変換する。store.py はこれ以外の例外(想定外のバグ)を
そのまま投げてよい(FastAPI が 500 にする)。
"""


class VaultError(Exception):
    """金庫の例外の基底クラス。"""


class NotFoundError(VaultError):
    """交渉・依頼者・テンプレートが見つからない(404)。"""


class MovePreconditionFailed(VaultError):
    """手の操作の前提が崩れている(409。§3.5)。

    expected_version の不一致・手番違い・状態(active でない)・一時停止中・
    トランザクションの再試行を使い切った(競合)のいずれか。このいずれでも、
    回数・記録を一切消費しない。レフェリーは 409 を受けたら状態を読み直す。
    """


class TransactionRetryExhausted(VaultError):
    """冪等な操作(control・expire)・作成のトランザクションが、競合で再試行を使い切った
    (503。再試行してよい)。手の操作(moves)は同じ場面でも 409 にする(MovePreconditionFailed)。
    """


class PolicyValidationError(VaultError):
    """ポリシーがグリッド外・矛盾などで拒否された(§3.3 の PUT policy)。"""
