"""受信メッセージの検証(design.md §4.3・§2.7、AC-04)。

各受信口は、次のすべてを満たすときだけ ADK の Runner を動かす。1 つでも違反したら
A2A のエラー(InvalidParamsError)を返し、LLM は一度も動かない。

- parts が 1 件だけで、それが DataPart(TextPart・ファイルの Part は受け付けない)。
- DataPart は `data` だけを受け付ける(台帳 X-41)。part の metadata(空は可)・ファイル名は拒否し、mediaType は、
  なしか `application/json` だけを受け付ける。自由な文字列を、A2A の層まで通さないため。
- `data` が、その受信口の型(TurnInput または AttackerTurnInput)として有効。
- メッセージの metadata は、`nid`(16 桁の 16 進数)だけを受け付ける。未知の項目は拒否する(台帳 X-41。自由な
  文字列を、A2A の層まで通さないため)。`nid` は省略してよい。LLM への入力には入れない。
- リクエストの metadata(`params.metadata`)は、空かなしだけを受け付ける。

検証の中身は negotiation_core.schema の型(TurnInput・AttackerTurnInput・Id)そのもので、
ここに別の検証規則は持たない(§4.3「検証コードは negotiation_core.schema の 1 つだけ」)。

エラーには、入力の値を含めない(違反した場所と種類だけ)。送り手が作れる項目名(metadata の未知の項目名・data の余分な
項目名)も含めない(台帳 X-43): 日本語の項目名に値を埋められるため。未知の項目は、`<unknown>` と件数と固定のエラーコード
(`extra_forbidden`)だけを返す。ファイル名・mediaType の値も返さない。エラーはログにも残り得るため、指示の自由文などが
漏れないようにする。
"""

import json
import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from a2a.types import Message, Part
from a2a.utils.errors import InvalidParamsError
from pydantic import TypeAdapter, ValidationError

from agents.wire import DATA_MEDIA_TYPE, part_kind, struct_to_python, value_to_python
from negotiation_core.schema import AttackerTurnInput, Id, TurnInput

# メッセージの metadata に載せてよい項目(§2.7)。今は交渉 ID だけ。これ以外の項目は拒否する(台帳 X-41)。
_MESSAGE_METADATA_KEYS = frozenset({"nid"})
_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(Id)

# part の mediaType として受け付ける値(なしか JSON だけ。台帳 X-41)。
_ACCEPTED_MEDIA_TYPES = frozenset({"", DATA_MEDIA_TYPE})

# エラーに載せる違反の最大件数と、場所の文字列の最大長。
_MAX_REPORTED_VIOLATIONS = 20
_MAX_FIELD_NAME_LENGTH = 80
_UNSAFE_FIELD_CHARS = re.compile(r"[^\w.\[\]-]")

# 送り手が作れる項目名(未知の項目・余分な項目の名前)の代わりに返す、固定の文字列(台帳 X-43)。
_UNKNOWN_NAME = "<unknown>"

_PART_KIND_NAMES = {"text": "TextPart", "raw": "FilePart(bytes)", "url": "FilePart(url)"}


def _reject(summary: str, violations: list[dict[str, str]] | None = None) -> InvalidParamsError:
    """InvalidParamsError を作る。violations は {field, message} の一覧(a2a-sdk の BadRequest の形)。"""
    if violations:
        detail = "; ".join(f"{v['field']}: {v['message']}" for v in violations[:5])
        return InvalidParamsError(message=f"{summary} ({detail})", data={"errors": violations})
    return InvalidParamsError(message=summary)


def _location(prefix: str, loc: Iterable[object], *, unknown: bool = False) -> str:
    """違反の場所の文字列。loc は、スキーマが決めた項目名と添字だけ(送り手が作れる名前は入れない)。

    unknown なら、末尾に `<unknown>` を付ける(余分な項目・未知の項目の、名前の代わり。台帳 X-43)。
    """
    parts = [_UNSAFE_FIELD_CHARS.sub("?", str(part)) for part in loc]
    if unknown:
        parts.append(_UNKNOWN_NAME)
    return ".".join(filter(None, [prefix, *parts]))[:_MAX_FIELD_NAME_LENGTH]


def _extra_forbidden(count: int) -> str:
    """余分な項目・未知の項目の違反の種類。固定のエラーコードに、件数を添える。"""
    return f"extra_forbidden (count={count})"


def _violations(exc: ValidationError, prefix: str = "") -> list[dict[str, str]]:
    """pydantic の違反を、場所と種類だけの一覧にする(入力値も、送り手が作れる項目名も含めない)。

    余分な項目(`extra_forbidden`)は、loc の末尾が、送り手が作った項目名なので、返さずに `<unknown>` に置き換え、同じ場所の
    ものを 1 件にまとめて件数を付ける(台帳 X-43)。それ以外の違反の loc は、スキーマが決めた項目名と添字だけ。
    """
    counts: dict[tuple[str, str], int] = {}
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        if error["type"] == "extra_forbidden":
            key = (_location(prefix, error["loc"][:-1], unknown=True), "extra_forbidden")
        else:
            key = (_location(prefix, error["loc"]), error["type"])
        counts[key] = counts.get(key, 0) + 1
    result = [
        {"field": field, "message": _extra_forbidden(count) if kind == "extra_forbidden" else kind}
        for (field, kind), count in counts.items()
    ]
    return result[:_MAX_REPORTED_VIOLATIONS]


def _unknown_fields(location: str, keys: Collection[str]) -> list[dict[str, str]]:
    """location の中の未知の項目の違反(1 件)。項目の名前は、送り手が作れるので返さず、`<unknown>` と件数だけを返す(台帳 X-43)。"""
    return [{"field": _location(location, (), unknown=True), "message": _extra_forbidden(len(keys))}]


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


def _check_empty_metadata(location: str, metadata: Mapping[str, Any]) -> None:
    """location の metadata(`params.metadata`・`parts[0].metadata`)は、空かなしだけを受け付ける(台帳 X-41)。

    中身があれば拒否する。項目の名前は、エラーに載せない(台帳 X-43)。
    """
    if metadata:
        raise _reject(f"{location} must be empty", _unknown_fields(location, metadata))


def _check_data_part_envelope(part: Part) -> None:
    """DataPart は、`data` だけを受け付ける(AC-04・台帳 X-41)。

    part の metadata(空は可)とファイル名は拒否し、mediaType は、なしか `application/json` だけを受け付ける。
    ファイル名・mediaType の値は、エラーの文に入れない。
    """
    if part.filename:
        raise _reject("parts[0].filename is not accepted")
    _check_empty_metadata("parts[0].metadata", struct_to_python(part.metadata))
    if part.media_type not in _ACCEPTED_MEDIA_TYPES:
        raise _reject(f"parts[0].mediaType must be absent or {DATA_MEDIA_TYPE}")


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

    _check_data_part_envelope(parts[0])
    _check_message_metadata(struct_to_python(message.metadata))
    _check_empty_metadata("params.metadata", request_metadata)

    data = value_to_python(parts[0].data)
    if not isinstance(data, dict):
        raise _reject("parts[0].data must be a JSON object")
    try:
        # 線の上のデータは JSON なので、JSON として検証する。スキーマは strict で、Python の辞書として
        # 検証すると、列挙型(Verdict)が文字列を受け付けない。strict のまま、JSON の文字列は通る。
        return input_model.model_validate_json(json.dumps(data))
    except ValidationError as exc:
        raise _reject(f"invalid {input_model.__name__}", _violations(exc)) from None
