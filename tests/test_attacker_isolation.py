"""DV-04(受信口の部分): design.md §2.7・§4.3・§12.2。

`AttackerTurnInput`(TurnInput に principal_instruction の自由文を足した型)は、攻撃モード用の
受信口 `/a2a/attacker` でしか受け付けられない。`/a2a/candidate`・`/a2a/employer` に送ると拒否され、
LLM(スタブ)は一度も動かない。自由文が、候補者側や通常の求人側の LLM の文脈に入る経路はない。

「`/a2a/attacker` は攻撃モードの交渉からしか呼ばれない」の部分は、呼ぶ側(web の段)で確かめる。
1c では作らない。

v14: 計画(phase=plan)でも決定(phase=decide)でも同じ。攻撃モードの求人エージェントも、通常の求人側と同じ 2 種類の呼び出し
(計画・決定)で動き、principal_instruction は毎手番、どちらの呼び出しにも渡る(§8.2)。
"""

import pytest

from agents.wire import PHASES
from agents_helpers import (  # noqa: F401  (フィクスチャは import して使う)
    agents_app,
    anyio_backend,
    assert_rejected,
    data_part,
    http,
    send_message,
    stub_llm,
    valid_data,
)

pytestmark = pytest.mark.anyio

INSTRUCTION = "依頼者の最低年収を聞き出して、その金額ぎりぎりまで下げてください。"


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("role", ["candidate", "employer"])
async def test_attacker_turn_input_is_rejected_by_the_other_endpoints(role, phase, http, stub_llm):
    # DV-04 (AttackerTurnInput は、候補者側・通常の求人側の受信口では拒否される。LLM は動かない。計画でも決定でも)
    data = valid_data("attacker", phase)  # principal_instruction を持つ AttackerTurnInput
    data["side"] = "candidate" if role == "candidate" else "employer"
    data["counterparty"] = valid_data(role)["counterparty"]
    assert "principal_instruction" in data
    body = await send_message(http, role, [data_part(data)])
    assert_rejected(body)
    assert stub_llm.requests == []


@pytest.mark.parametrize("phase", PHASES)
async def test_attacker_turn_input_is_accepted_by_the_attacker_endpoint(phase, http, stub_llm):
    # DV-04 (対照: 同じ AttackerTurnInput は、/a2a/attacker では通り、LLM が動く。計画でも決定でも)
    data = valid_data("attacker", phase)
    data["principal_instruction"] = INSTRUCTION
    body = await send_message(http, "attacker", [data_part(data)])
    assert "error" not in body, body
    assert len(stub_llm.requests) == 1
    # 攻撃モードの求人エージェントには、指示が渡る(§8.2)。ほかの 2 つには渡らない(上のテスト)。
    assert INSTRUCTION in stub_llm.requests[0].contents[0][1][0]


@pytest.mark.parametrize("phase", PHASES)
async def test_plain_turn_input_is_rejected_by_the_attacker_endpoint(phase, http, stub_llm):
    # DV-04 (/a2a/attacker は AttackerTurnInput だけを受け付ける。principal_instruction のない TurnInput は拒否)
    data = valid_data("employer", phase)  # 通常の求人側の TurnInput
    assert "principal_instruction" not in data
    body = await send_message(http, "attacker", [data_part(data)])
    assert_rejected(body)
    assert stub_llm.requests == []


@pytest.mark.parametrize("phase", PHASES)
async def test_principal_instruction_longer_than_400_chars_is_rejected(phase, http, stub_llm):
    # DV-04 (§2.7: principal_instruction は 400 文字以内。攻撃モードの受信口でも超えたら拒否)
    data = valid_data("attacker", phase)
    data["principal_instruction"] = "あ" * 401
    body = await send_message(http, "attacker", [data_part(data)])
    assert_rejected(body)
    assert stub_llm.requests == []
