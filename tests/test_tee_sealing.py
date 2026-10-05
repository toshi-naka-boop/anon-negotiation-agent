"""TEE 版の金庫の封印(src/vault/tee/sealing.py。research/tee-spike-contract.md §6)。

確かめること:
- AES-256-GCM の往復。出力は `nonce(12) || 暗号文(タグ込み)` で、nonce は毎回新しい。
- AAD は `f"{path}#{field}"`: 文書のパスか項目名が違うと、開けない(運営者が暗号文を別の文書・項目に差し替えても、開封できない)。
- 鍵違い・書き換え(nonce・本体・タグのどこでも)・短すぎる入力は SealError(ValueError の一種)。文に平文も暗号文も入らない。
- DEK は 32 バイトだけ(それ以外は ValueError)。
- NoopSealer は同じ API で、そのまま返す。
"""

import inspect
import os

import pytest

from vault.tee.sealing import NoopSealer, SealError, Sealer

PATH = "principals/0123456789abcdef"
FIELD = "policy"
PLAINTEXT = b"rounded policy: salary 700, remote_days 2"


@pytest.fixture
def sealer() -> Sealer:
    return Sealer(os.urandom(32))


def test_round_trip(sealer):
    sealed = sealer.seal(PATH, FIELD, PLAINTEXT)

    assert sealer.open(PATH, FIELD, sealed) == PLAINTEXT


@pytest.mark.parametrize("plaintext", [b"", b"x", b"\x00" * 1000, os.urandom(1 << 20)])
def test_round_trip_for_any_length(sealer, plaintext):
    assert sealer.open(PATH, FIELD, sealer.seal(PATH, FIELD, plaintext)) == plaintext


def test_the_layout_is_a_12_byte_nonce_then_the_ciphertext_with_its_tag(sealer):
    sealed = sealer.seal(PATH, FIELD, PLAINTEXT)

    assert len(sealed) == 12 + len(PLAINTEXT) + 16
    assert PLAINTEXT not in sealed


def test_every_seal_uses_a_fresh_nonce(sealer):
    sealed = [sealer.seal(PATH, FIELD, PLAINTEXT) for _ in range(50)]

    assert len({value[:12] for value in sealed}) == 50  # nonce が重ならない
    assert len(set(sealed)) == 50  # 同じ平文でも、封印の結果は毎回違う


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("principals/ffffffffffffffff", FIELD),  # 別の文書へ差し替えた
        (PATH, "blocklist"),  # 別の項目へ差し替えた
        ("negotiations/0123456789abcdef", FIELD),
    ],
)
def test_a_different_path_or_field_cannot_open_it(sealer, path, field):
    sealed = sealer.seal(PATH, FIELD, PLAINTEXT)

    with pytest.raises(SealError):
        sealer.open(path, field, sealed)


def test_a_different_key_cannot_open_it(sealer):
    sealed = sealer.seal(PATH, FIELD, PLAINTEXT)

    with pytest.raises(SealError):
        Sealer(os.urandom(32)).open(PATH, FIELD, sealed)


@pytest.mark.parametrize("position", ["nonce", "body", "tag"])
def test_a_tampered_value_cannot_be_opened(sealer, position):
    sealed = bytearray(sealer.seal(PATH, FIELD, PLAINTEXT))
    index = {"nonce": 0, "body": 12, "tag": len(sealed) - 1}[position]
    sealed[index] ^= 0x01

    with pytest.raises(SealError):
        sealer.open(PATH, FIELD, bytes(sealed))


@pytest.mark.parametrize("length", [0, 1, 11, 12, 27])  # nonce(12) + タグ(16) = 28 バイト未満
def test_a_value_shorter_than_nonce_and_tag_is_a_seal_error(sealer, length):
    with pytest.raises(SealError):
        sealer.open(PATH, FIELD, os.urandom(length))


def test_the_smallest_valid_value_is_an_empty_plaintext(sealer):
    sealed = sealer.seal(PATH, FIELD, b"")

    assert len(sealed) == 28
    assert sealer.open(PATH, FIELD, sealed) == b""


def test_seal_error_is_a_value_error_and_does_not_carry_the_data(sealer):
    sealed = sealer.seal(PATH, FIELD, PLAINTEXT)
    tampered = bytes([sealed[0] ^ 0x01]) + sealed[1:]

    with pytest.raises(SealError) as raised:
        sealer.open(PATH, FIELD, tampered)

    assert isinstance(raised.value, ValueError)
    message = str(raised.value)
    assert PLAINTEXT.decode() not in message
    assert tampered.hex() not in message and repr(tampered) not in message
    assert raised.value.__cause__ is None and raised.value.__suppress_context__  # 元の例外を鎖にしない


@pytest.mark.parametrize("dek", [b"", b"x" * 16, b"x" * 31, b"x" * 33, b"x" * 64, "x" * 32, None])
def test_the_dek_must_be_exactly_32_bytes(dek):
    with pytest.raises(ValueError):
        Sealer(dek)


def test_the_dek_is_not_in_the_error_of_a_wrong_length():
    dek = b"secret-dek-of-the-wrong-length"

    with pytest.raises(ValueError) as raised:
        Sealer(dek)

    assert dek.decode() not in str(raised.value)


def test_noop_sealer_returns_the_input_as_it_is():
    noop = NoopSealer()

    assert noop.seal(PATH, FIELD, PLAINTEXT) == PLAINTEXT
    assert noop.open(PATH, FIELD, PLAINTEXT) == PLAINTEXT
    assert noop.open(PATH, FIELD, noop.seal(PATH, FIELD, b"")) == b""


def test_noop_sealer_has_the_same_api_as_sealer():
    for name in ("seal", "open"):
        assert inspect.signature(getattr(NoopSealer, name)).parameters.keys() == inspect.signature(
            getattr(Sealer, name)
        ).parameters.keys()
