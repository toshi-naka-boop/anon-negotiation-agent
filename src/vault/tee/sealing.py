"""項目の封印(AES-256-GCM。research/tee-spike-contract.md §6、research/tee-spike.md 2-5)。

Firestore に書く機微な項目を、DEK(データ暗号鍵。メモリにだけ置く)で項目ごとに封印する。保存形式は `nonce(12) || 暗号文(タグ込み)`。
AAD は `f"{path}#{field}"`(path は Firestore の文書パス)なので、運営者が暗号文を別の文書・別の項目に差し替えても開封できない。

store.py は、vault.seal_layer を通して、本物の依頼者と live の交渉の機微な項目を封印する(design.md §9 の 2)。TEE 版の起動口(vault.tee.main)が、
この Sealer を VaultStore に渡し、封印の往復を確かめる自己試験にも使う。Cloud Run 版・テストは、そのまま返す NoopSealer を使う(保存の形は変わらない)。

SealError と ValueError のメッセージに、平文・暗号文・鍵は入れない。
"""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEK_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16


class SealError(ValueError):
    """封印を開けられなかった(鍵・文書のパス・項目名が違う、壊れている、短すぎる)。平文も暗号文も文に入れない。"""


def _aad(path: str, field: str) -> bytes:
    return f"{path}#{field}".encode()


class Sealer:
    """DEK(32 バイト)で、項目を封印する・開封する。"""

    def __init__(self, dek: bytes) -> None:
        if not isinstance(dek, bytes) or len(dek) != DEK_BYTES:
            raise ValueError(f"the DEK must be {DEK_BYTES} bytes")
        self._aead = AESGCM(dek)

    def seal(self, path: str, field: str, plaintext: bytes) -> bytes:
        """plaintext を封印する。毎回 12 バイトの乱数の nonce を新しく作り、`nonce || 暗号文(タグ込み)` を返す。"""
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext, _aad(path, field))

    def open(self, path: str, field: str, sealed: bytes) -> bytes:
        """封印を開ける。タグが合わない・AAD(path・field)が違う・短すぎる場合は SealError。"""
        if len(sealed) < _NONCE_BYTES + _TAG_BYTES:
            raise SealError("the sealed value is too short")
        try:
            return self._aead.decrypt(sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:], _aad(path, field))
        except InvalidTag:
            raise SealError("the sealed value could not be opened") from None


class NoopSealer:
    """Sealer と同じ API で、そのまま返す(Cloud Run 版・テスト用)。"""

    def seal(self, path: str, field: str, plaintext: bytes) -> bytes:
        return plaintext

    def open(self, path: str, field: str, sealed: bytes) -> bytes:
        return sealed
