"""ログから ID を外す設定(design.md §3.8。台帳 X-40)。web と vault の本番の起動口が、起動時に呼ぶ。

§3.8: ログに残すのは、エンドポイント・side・手の種類・回数・判定結果だけ。依頼者 ID・交渉 ID(16 桁の 16 進数。§2.7)は、
ログが本人の削除の後も残り、ID から交渉の時刻・失敗の理由・側をたどれてしまうので、書かない。

URL のパスに ID が入る uvicorn のアクセスログ(web の `/v1/principals/{pid}/...`・vault の `/v1/negotiations/{nid}/...`)は、
アプリのコードではなく uvicorn が書くので、フィルタで ID を伏せる。web と vault の両方がこの 1 か所を使う(伏せ方が
2 か所で食い違わないように)。negotiation_core に置くのは、vault が web に依存せず、web も vault に依存せずに、
両方が共有できる場所だから(vault・agents・web が、すでにすべて依存している)。
Cloud Run 自身のリクエストログ(run.googleapis.com/requests)は、URL をそのまま持ち、アプリからは変えられないので、
デプロイの設定で扱う(台帳 I-9)。
"""

import logging
import re

# 16 桁の 16 進数(依頼者 ID・交渉 ID。§2.7 の ID_PATTERN と同じ形)。前後が 16 進数の文字でないものだけ。
_ID_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")
# 作成の冪等キーが URL のパスに入る口(金庫の `GET /v1/negotiations/by-request/{request_id}`。§3.3・台帳 X-57)。
# 冪等キーは依頼者 ID を含み得るので、`by-request/` の後ろをまとめて伏せる(空白か引用符まで。パーセントエンコードも含む)。
_REQUEST_KEY_IN_PATH = re.compile(r"(/by-request/)[^\s\"']+")


class _MaskIdsFilter(logging.Filter):
    """ログの引数の文字列にある ID(16 桁の 16 進数)を `<id>` に置き換える(台帳 X-40)。

    uvicorn のアクセスログは、`'%s - "%s %s HTTP/%s" %d'`(クライアント・メソッド・URL・版・ステータス)の形で、
    URL(/v1/principals/{依頼者 ID}/...・/v1/negotiations/{交渉 ID}/...)をそのまま書く。引数の並びは変えない。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_mask(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def _mask(text: str) -> str:
    """文字列の中の ID と、冪等キーの口のパスを伏せる。"""
    return _ID_IN_TEXT.sub("<id>", _REQUEST_KEY_IN_PATH.sub(r"\1<key>", text))


def mask_ids_in_logs() -> None:
    """URL に ID が入るログから、ID を外す(台帳 X-40)。

    §3.8: ログに残すのは、エンドポイント・side・手の種類・回数・判定結果だけ。
    - uvicorn のアクセスログ(`uvicorn.access`): URL の ID を `<id>` に伏せる(エンドポイントとステータスは残す)。
    - httpx のリクエストのログ: INFO で、金庫への呼び出しの URL(ID つき)を書くので、WARNING 以上にする(web が金庫を呼ぶ側)。
    本番の起動口(web.app.create_app_from_env・vault.app.create_app_from_env)が呼ぶ。何度呼んでも、フィルタは 1 つだけ。
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, _MaskIdsFilter) for existing in access_logger.filters):
        access_logger.addFilter(_MaskIdsFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
