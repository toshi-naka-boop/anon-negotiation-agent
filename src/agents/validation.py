"""受信メッセージの検証(design.md §4.3・§2.7、AC-04)。

各受信口は、次のすべてを満たすときだけ ADK の Runner を動かす。1 つでも違反したら
A2A のエラー(InvalidParamsError)を返し、LLM は一度も動かない。

- parts が 1 件だけで、それが DataPart(TextPart・ファイルの Part は受け付けない)。
- `data` が、その受信口の型(TurnInput または AttackerTurnInput)として有効。
- ID(`nid`)は metadata から取り、形式(16 桁の 16 進数)を検査する。LLM への入力には入れない。

検証の中身は negotiation_core.schema の型(TurnInput・AttackerTurnInput・Id)そのもので、
ここに別の検証規則は持たない(§4.3「検証コードは negotiation_core.schema の 1 つだけ」)。

エラーには、入力の値を含めない(違反した場所と種類だけ)。エラーはログにも残るため、
指示の自由文などが漏れないようにする。
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from a2a.types import Message
from a2a.utils.errors import InvalidParamsError
from pydantic import TypeAdapter, ValidationError

from agents.wire import part_kind, struct_to_python, value_to_python
from negotiation_core.schema import AttackerTurnInput, Id, TurnInput

# metadata に載せる ID のキー(§2.7)。今は交渉 ID だけ。
_ID_METADATA_KEYS = ("nid",)
_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(Id)

# エラーに載せる違反の最大件数と、場所の文字列の最大長(送り手が場所の名前を作れるため)。
_MAX_REPORTED_VIOLATIONS = 20
_MAX_FIELD_NAME_LENGTH = 80
_UNSAFE_FIELD_CHARS = re.compile(r"[^\w.\[\]-]")

_PART_KIND_NAMES = {"text": "TextPart", "raw": "FilePart(bytes)", "url": "FilePart(url)"}


def _reject(summary: str, violations: list[dict[str, str]] | None = None) -> InvalidParamsError:
    """InvalidParamsError を作る。violations は {field, message} の一覧(a2a-sdk の BadRequest の形)。"""
    if violations:
        detail = "; ".join(f"{v['field']}: {v['message']}" for v in violations[:5])
        return InvalidParamsError(message=f"{summary} ({detail})", data={"errors": violations})
    return InvalidParamsError(message=summary)


def _violations(exc: ValidationError, prefix: str = "") -> list[dict[str, str]]:
    """pydantic の違反を、場所と種類だけの一覧にする(入力値は含めない)。"""
    result = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        field = prefix + ".".join(str(part) for part in error["loc"])
        field = _UNSAFE_FIELD_CHARS.sub("?", field)[:_MAX_FIELD_NAME_LENGTH]
        result.append({"field": field, "message": error["type"]})
    return result[:_MAX_REPORTED_VIOLATIONS]


def _check_ids(metadata: Mapping[str, Any], where: str) -> None:
    """metadata に ID があれば、形式を検査する(ID の形式違反は拒否。AC-04)。"""
    for key in _ID_METADATA_KEYS:
        if key not in metadata:
            continue
        try:
            _ID_ADAPTER.validate_python(metadata[key])
        except ValidationError as exc:
            raise _reject(f"invalid ID in {where}", _violations(exc, prefix=f"{where}.{key}")) from None


def validate_request(
    message: Message | None,
    request_metadata: Mapping[str, Any],
    input_model: type[TurnInput],
) -> TurnInput:
    """受信メッセージを検証し、その受信口の型(input_model)の値にして返す。

    違反したら InvalidParamsError(A2A のエラー)を投げる。input_model は TurnInput か
    AttackerTurnInput(AttackerTurnInput は攻撃モード用の受信口だけが使う。§4.3)。
    """
    if message is None:
        raise _reject("message is missing")

    parts = list(message.parts)
    if len(parts) != 1:
        raise _reject(f"message must contain exactly one DataPart (got {len(parts)} parts)")
    kind = part_kind(parts[0])
    if kind != "data":
        name = _PART_KIND_NAMES.get(kind or "", "an empty part")
        raise _reject(f"parts[0] is {name}; only a DataPart is accepted")

    _check_ids(struct_to_python(message.metadata), "message.metadata")
    _check_ids(request_metadata, "params.metadata")

    data = value_to_python(parts[0].data)
    if not isinstance(data, dict):
        raise _reject("parts[0].data must be a JSON object")
    try:
        # 線の上のデータは JSON なので、JSON として検証する。スキーマは strict で、Python の辞書として
        # 検証すると、列挙型(Verdict)が文字列を受け付けない。strict のまま、JSON の文字列は通る。
        return input_model.model_validate_json(json.dumps(data))
    except ValidationError as exc:
        raise _reject(f"invalid {input_model.__name__}", _violations(exc)) from None
