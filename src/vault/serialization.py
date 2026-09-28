"""pydantic モデル ⇔ Firestore の素の dict の変換(design.md §3.1・§3.2)。

Firestore のクライアントは datetime をそのまま Timestamp として扱えるので datetime は
変換しない。Enum(Verdict など)だけ値(str)に変換する。これを共通化することで、
負債やテンプレートなど、金庫が書き込むすべての文書で同じ変換を使い回す。
"""

from enum import Enum
from typing import TypeVar

from pydantic import BaseModel

_M = TypeVar("_M", bound=BaseModel)


def _convert(value):
    if isinstance(value, BaseModel):
        return _convert(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {k: _convert(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value


def model_to_firestore(model: BaseModel) -> dict:
    """pydantic モデルを、Firestore にそのまま書き込める dict にする。"""
    return _convert(model.model_dump(mode="python"))


def model_from_firestore(model_cls: type[_M], data: dict) -> _M:
    """Firestore から読んだ dict をモデルに戻す。

    negotiation_core の StrictModel は strict=True だが、Firestore は Enum を素の str
    (例: Verdict.ACCEPTABLE → "acceptable")として保存するため、strict=True のままだと
    読み戻しに失敗する(str から Enum への型強制が禁じられるため)。ここで読むデータは、
    書き込み時に一度検証済みのものなので、strict=False で読み戻してよい。
    """
    return model_cls.model_validate(data, strict=False)
