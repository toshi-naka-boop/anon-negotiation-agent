"""3 枚の壁の実演(design.md §8.1)のうち、壁 2 と壁 3 の応答を作る(壁 1 は web.attack.raw_message)。

実演は「何を送ったら、どこで、どう止まったか」を返す JSON。

- 壁 2(llm_context_report): 候補者側エージェントの直近の手番について、LLM の文脈の全文を返す。固定の前文(指示文＋グリッド。
  agents.instructions の candidate)と、LLM に渡した TurnInput(計画と決定の 2 回ぶん)を、そのまま。あわせて、その入力を機械的に調べた結果
  (inspect_llm_input)を返す: 自由文・ID・グリッド外の数値(生の値)が入っていない = 3 つとも空。「見える」だけでなく、確かめられる形にする。
  LLM に渡る文脈は「前文＋TurnInput」だけ(§4.2)なので、これで全文になる。
- 壁 3(vault_answers_report): 攻撃者の提案ごとに、候補者側の金庫が何を答えたか。答えは「受けられる／受けられない／本人確認が必要」の
  3 値だけ(丸め済み。値も境目も返さない)。相手は架空人物なので、候補者側の見え方を画面に出してよい(§3.2)。区間の計算(推定区間
  メーター。§8.3)は別の API の仕事で、ここは、金庫の答えそのものを示す。
"""

import json
import re
from typing import Any, get_args

from negotiation_core import (
    ATTRIBUTE_BANDS,
    AXES,
    CATEGORICAL_AXIS_KEYS,
    ID_PATTERN,
    NUMERIC_AXIS_KEYS,
    LastErrorReason,
    MoveType,
    Phase,
    Side,
    Verdict,
)

from vault.api_models import EventViewItem

# LLM に渡す入力(TurnInput の JSON)の文字列の値として、あってよいもの(スキーマが決めた列挙値だけ)。これ以外の文字列は自由文とみなす。
_ALLOWED_STRINGS: frozenset[str] = frozenset(
    {
        "turn-input/v1",
        "self",
        "counterparty",
        *get_args(Side),
        *get_args(Phase),
        *get_args(MoveType),
        *get_args(LastErrorReason),
        *(verdict.value for verdict in Verdict),
        *(value for band in ATTRIBUTE_BANDS.values() for value in band.grid),
        *(value for axis in CATEGORICAL_AXIS_KEYS for value in AXES[axis].grid),
    }
)
_ID_IN_TEXT = re.compile(ID_PATTERN.removeprefix("^").removesuffix("$"))  # 16 桁の 16 進数(文字列のどこにあっても)
_MAX_REPORTED_VALUE_CHARS = 80  # 見つけた値を報告するときの長さの上限(報告そのものが大きくならないように)
PHASES: tuple[Phase, ...] = get_args(Phase)


def inspect_llm_input(text: str) -> dict[str, list]:
    """LLM に渡した入力(TurnInput の JSON の文字列)に、自由文・ID・グリッド外の数値が入っていないかを調べる(壁 2)。

    - free_text: スキーマの列挙値でない文字列の値(自由文)。
    - ids: 16 桁の 16 進数(ID の形)を含む文字列の値。
    - off_grid_numbers: 数値軸の値のうち、グリッドにない数値(丸める前の生の値は、グリッドの目盛りの間にある)。
    3 つとも空なら、入っていない。項目名(キー)は見ない(スキーマが決めている)。
    """
    found: dict[str, list] = {"free_text": [], "ids": [], "off_grid_numbers": []}

    def walk(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, child_key)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str):
            if value not in _ALLOWED_STRINGS:
                found["free_text"].append(value[:_MAX_REPORTED_VALUE_CHARS])
            if _ID_IN_TEXT.search(value):
                found["ids"].append(value[:_MAX_REPORTED_VALUE_CHARS])
        elif isinstance(value, int | float) and not isinstance(value, bool):
            if key in NUMERIC_AXIS_KEYS and value not in AXES[key].grid:
                found["off_grid_numbers"].append({"axis": key, "value": value})

    walk(json.loads(text))
    return found


def llm_context_report(preamble: str, texts: dict[Phase, str]) -> dict[str, Any]:
    """壁 2 の応答。preamble は固定の前文、texts は LLM に渡した TurnInput の JSON の文字列(phase → 文字列。覚えている分だけ)。"""
    inspection = {phase: inspect_llm_input(texts[phase]) for phase in PHASES if phase in texts}
    return {
        "wall": 2,
        "agent": "candidate",
        "preamble": preamble,
        "turn_inputs": {phase: texts.get(phase) for phase in PHASES},
        "inspection": inspection,
        "clean": all(not any(result.values()) for result in inspection.values()),
    }


def vault_answers_report(candidate_events: list[EventViewItem]) -> dict[str, Any]:
    """壁 3 の応答。candidate_events は、候補者側の見え方のイベント列。攻撃者の提案(offer_received)ごとの、金庫の答えを並べる。"""
    answers = [
        {"seq": event.seq, "package": event.package.model_dump(mode="json"), "vault_answer": event.own_evaluation}
        for event in candidate_events
        if event.kind == "offer_received" and event.package is not None
    ]
    return {"wall": 3, "answer_values": [verdict.value for verdict in Verdict], "answers": answers}
