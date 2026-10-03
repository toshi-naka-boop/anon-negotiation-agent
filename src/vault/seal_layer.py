"""封印レイヤ: Firestore の文書の、封印する項目を、書く前に封印し、読んだ後に開く入口(design.md §9 の 2「何を暗号化するか」・DV-19)。

VaultStore は、封印の対象になりうる文書(`principals/{pid}`・`negotiations/{nid}`・`negotiations/{nid}/events/{version}`)の、封印する項目を、
必ずこのレイヤを通して書き・読む(store.py の `_read_negotiation`・`_write_negotiation`・`_read_principal`・`_merge_principal`・
`_record_event`・`_read_event_items`)。

- 封印するのは、本物の依頼者の文書と、mode=live の交渉とそのイベントだけ。項目は下の定数のとおり(§9 の 2 の表)。
  デモ・攻撃の交渉とそのイベント・テンプレートは、公開フィクスチャなので封印しない。冪等キーの文書(nid と時刻だけ)も封印しない。
- 索引・制御に使う項目(依頼者 ID・テンプレート ID・job_id・架空かどうか・status・end_reason・to_move・paused・期限・version・seq・mode・
  request_id・TTL・カウンタ)は平文のまま残す。クエリ(`participants.*.principal_id`・`status in [...]`・`views.<side>.seq > n`)が使う。
- 封印: 項目の値を JSON(pydantic の JSON モード)の bytes にして Sealer で封印し、Firestore の bytes 型で保存する。
  AAD は `f"{文書のパス}#{項目名}"`。項目名は文書の中の項目のパス(例 `participants.candidate.attribute_bands`・`views.employer.payload`)。
  値が None の項目も封印する(None かどうかを平文に残さない)。nonce は封印のたびに新しいので、トランザクションが再試行されて
  同じ中身を封印し直しても、読み戻した結果は変わらない。
- イベントの `views.<side>` は、封印するとき `{seq, payload}` になる(payload は seq 以外の項目の JSON。seq は平文)。
  封印しないときは、seq と項目が並んだ従来の形のまま。
- NoopSealer のときは何もしない(保存の形は、封印のない版と変わらない。Cloud Run 版と既存のテスト)。
- 本物の Sealer のときに、封印する項目が bytes でなければ(平文に書き換えられた)、SealError にする(平文を黙って受け入れない)。
"""

import json
from typing import Any

from pydantic import TypeAdapter

from negotiation_core import Side

from vault.models import EventRecord, EventView, NegotiationDocument, NegotiationMode
from vault.serialization import model_from_firestore, model_to_firestore
from vault.tee.sealing import NoopSealer, SealError, Sealer

# §9 の 2 の表。項目名は文書の中のパス(. 区切り)で、AAD の「項目名」になる。
PRINCIPAL_SEALED_FIELDS = ("policy", "blocklist", "removed_axes", "attribute_bands")
NEGOTIATION_SEALED_FIELDS = ("snapshots", "pending_offer", "last_check", "pending_question", "result")
CANDIDATE_BANDS_FIELD = "participants.candidate.attribute_bands"

_JSON = TypeAdapter(Any)  # 値を pydantic の JSON モードで JSON の bytes にする(モデル・Enum・日時もそのまま渡せる)


def view_payload_field(side: Side) -> str:
    """イベントの `views.<side>` の、封印する項目の名前(AAD の項目名)。"""
    return f"views.{side}.payload"


class SealLayer:
    """Sealer を使う、封印する項目の書き込み・読み込みの入口。"""

    def __init__(self, sealer: Sealer | NoopSealer) -> None:
        self._sealer = sealer
        self._active = not isinstance(sealer, NoopSealer)

    def _seals(self, mode: NegotiationMode | None) -> bool:
        return self._active and mode == "live"

    def _seal(self, path: str, field: str, value: Any) -> bytes:
        return self._sealer.seal(path, field, _JSON.dump_json(value))

    def _open(self, path: str, field: str, stored: Any) -> Any:
        if not isinstance(stored, bytes):
            raise SealError(f"the field {field!r} of a sealed document is not sealed")  # 平文の値は文に入れない
        return json.loads(self._sealer.open(path, field, stored))

    # --- principals/{pid}(本物の依頼者。部分更新の dict でも、読んだ dict でもよい) ---

    def principal_to_firestore(self, path: str, data: dict) -> dict:
        if not self._active:
            return data
        return {
            name: self._seal(path, name, value) if name in PRINCIPAL_SEALED_FIELDS else value
            for name, value in data.items()
        }

    def principal_from_firestore(self, path: str, data: dict) -> dict:
        if not self._active:
            return data
        return {
            name: self._open(path, name, value) if name in PRINCIPAL_SEALED_FIELDS else value
            for name, value in data.items()
        }

    # --- negotiations/{nid} ---

    def negotiation_to_firestore(self, path: str, doc: NegotiationDocument) -> dict:
        data = model_to_firestore(doc)
        if not self._seals(doc.mode):
            return data
        for name in NEGOTIATION_SEALED_FIELDS:
            data[name] = self._seal(path, name, data[name])
        candidate = data["participants"]["candidate"]
        candidate["attribute_bands"] = self._seal(path, CANDIDATE_BANDS_FIELD, candidate["attribute_bands"])
        return data

    def negotiation_from_firestore(self, path: str, data: dict) -> NegotiationDocument:
        if self._seals(data.get("mode")):
            participants = data["participants"]
            candidate = {
                **participants["candidate"],
                "attribute_bands": self._open(
                    path, CANDIDATE_BANDS_FIELD, participants["candidate"].get("attribute_bands")
                ),
            }
            data = {
                **data,
                **{name: self._open(path, name, data.get(name)) for name in NEGOTIATION_SEALED_FIELDS},
                "participants": {**participants, "candidate": candidate},
            }
        return model_from_firestore(NegotiationDocument, data)

    # --- negotiations/{nid}/events/{version}(mode は、親の交渉の mode) ---

    def event_to_firestore(self, path: str, record: EventRecord, mode: NegotiationMode) -> dict:
        data = model_to_firestore(record)
        if not self._seals(mode):
            return data
        for side, view in list(data["views"].items()):
            if view is not None:
                payload = {name: value for name, value in view.items() if name != "seq"}
                sealed = self._seal(path, view_payload_field(side), payload)
                data["views"][side] = {"seq": view["seq"], "payload": sealed}
        return data

    def event_view_from_firestore(self, path: str, side: Side, view: dict, mode: NegotiationMode) -> EventView:
        if self._seals(mode):
            payload = self._open(path, view_payload_field(side), view.get("payload"))
            view = {**payload, "seq": view["seq"]}
        return model_from_firestore(EventView, view)
