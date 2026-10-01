"""受信メッセージの検証(design.md §4.3・§2.7、AC-04)。

各受信口は、次のすべてを満たすときだけ ADK の Runner を動かす。1 つでも違反したら
A2A のエラー(InvalidParamsError)を返し、LLM は一度も動かない。

- parts が 1 件だけで、それが DataPart(TextPart・ファイルの Part は受け付けない)。
- `data` が、その受信口の型(TurnInput または AttackerTurnInput)として有効。
- メッセージの metadata は、`nid`(16 桁の 16 進数)だけを受け付ける。未知の項目は拒否する(台帳 X-41。自由な
  文字列を、A2A の層まで通さないため)。`nid` は省略してよい。LLM への入力には入れない。
- リクエストの metadata(`params.metadata`)は、空かなしだけを受け付ける。

検証の中身は negotiation_core.schema の型(TurnInput・AttackerTurnInput・Id)そのもので、
ここに別の検証規則は持たない(§4.3「検証コードは negotiation_core.schema の 1 つだけ」)。

エラーには、入力の値を含めない(違反した場所と種類だけ)。エラーはログにも残るため、
指示の自由文などが漏れないようにする。
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from a2a.types import Message
from a2a.utils.errors import InvalidParamsError
from pydantic import TypeAdapter, ValidationError

from agents.wire import part_kind, struct_to_python, value_to_python
from negotiation_core.schema import AttackerTurnInput, Id, TurnInput

# メッセージの metadata に載せてよい項目(§2.7)。今は交渉 ID だけ。これ以外の項目は拒否する(台帳 X-41)。
_MESSAGE_METADATA_KEYS = frozenset({"nid"})
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


def _unknown_fields(prefix: str, keys: Iterable[str]) -> list[dict[str, str]]:
    """未知の項目の違反の一覧(場所と種類だけ。値は載せない)。

    送り手が項目の名前を作れるので、名前は、文字と長さを絞った形で載せる(`_violations` と同じ扱い)。
    """
    return [
        {"field": _UNSAFE_FIELD_CHARS.sub("?", f"{prefix}.{key}")[:_MAX_FIELD_NAME_LENGTH], "message": "extra_forbidden"}
        for key in keys
    ][:_MAX_REPORTED_VIOLATIONS]


def _check_message_metadata(metadata: Mapping[str, Any]) -> None:
    """message.metadata は、`nid`(16 桁の 16 進数)だけを受け付ける。未知の項目・ID の形式違反は拒否(AC-04・台帳 X-41)。"""
    unknown = [key for key in metadata if key not in _MESSAGE_METADATA_KEYS]
    if unknown:
        raise _reject("unknown field in message.metadata", _unknown_fields("message.metadata", unknown))
    if "nid" in metadata:
        try:
            _ID_ADAPTER.validate_python(metadata["nid"])
        except ValidationError as exc:
            raise _reject("invalid ID in message.metadata", _violations(exc, prefix="message.metadata.nid")) from None


def _check_request_metadata(metadata: Mapping[str, Any]) -> None:
    """リクエストの metadata(`params.metadata`)は、空かなしだけを受け付ける(台帳 X-41)。"""
    if metadata:
        raise _reject("params.metadata must be empty", _unknown_fields("params.metadata", metadata))


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

    _check_message_metadata(struct_to_python(message.metadata))
    _check_request_metadata(request_metadata)

    data = value_to_python(parts[0].data)
    if not isinstance(data, dict):
        raise _reject("parts[0].data must be a JSON object")
    try:
        # 線の上のデータは JSON なので、JSON として検証する。スキーマは strict で、Python の辞書として
        # 検証すると、列挙型(Verdict)が文字列を受け付けない。strict のまま、JSON の文字列は通る。
        return input_model.model_validate_json(json.dumps(data))
    except ValidationError as exc:
        raise _reject(f"invalid {input_model.__name__}", _violations(exc)) from None
