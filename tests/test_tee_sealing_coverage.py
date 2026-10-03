"""DV-19: 封印の対象の項目が、vault-db の全文書のどこにも平文で現れないこと(design.md §9 の 2・§12.2。src/vault/seal_layer.py)。

本物の Sealer(乱数の DEK)で VaultStore を組み、live の依頼者 2 人(候補者・求人)で交渉を進めて途中確認に答え、エミュレータの
vault-db の全文書(principals・negotiations・events・冪等キー・テンプレート)を dict として全探索する。確かめること:

(a) 封印の対象の項目は、どの文書でも bytes で、平文の dict・list・str ではない(イベントは views.<側>.payload。seq は平文)。
(b) 平文のまま残る項目の集合(文書の種類ごと)が、§9 の 2 の列挙と一致する。列挙にない項目が平文なら失敗する。
(c) カナリア(候補者のポリシーの目印の組み合わせ・回答した組み合わせ・属性帯・外した軸・ブロック先)は、bytes 以外の値を
    JSON にした文字列のどこにも現れない。
(d) 同じ操作を NoopSealer で行うと、(a) が成り立たず、カナリアが平文で見つかる(試験が本当に封印を見ていることの対照)。
(e) 封印した文書を別の DEK の Sealer で読むと SealError になる。

あわせて、AAD が「文書のパス + 項目名」であること、暗号文を別の文書・項目に移しても、平文に書き換えても読めないこと、
トランザクションの再試行で封印をやり直しても結果が同じで nonce が新しいこと、デモ・攻撃・テンプレートは封印しないこと、
本人の削除の後始末が封印したままで動くこと、TEE の起動口が DEK の Sealer を VaultStore に渡すことを確かめる。

交渉は 2 つ進める。A: 本物の候補者 対 架空の求人(金庫の API が作れる実際の経路)。B: 求人側も本物の依頼者(金庫の API は求人側を
常にテンプレートにするので、交渉の文書を封印レイヤ経由で直接置く。tests/test_principal_deletion.py と同じ扱い)。
"""

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1.transaction import Transaction
from negotiation_core import ATTRIBUTE_BANDS, AXIS_KEYS, Anchor, CandidateAttributeBands, Package, Policy

import vault.tee.main as main_module
from vault.api_models import MoveRequest, PrincipalAnswerRequest, PutBlocklistRequest, PutPolicyRequest
from vault.app import create_app_from_env
from vault.clock import FixedClock
from vault.config import DEFAULT_VAULT_CONFIG
from vault.ids import generate_id
from vault.models import NegotiationDocument, Participant, Participants, Snapshots
from vault.seal_layer import SealLayer
from vault.store import VaultStore
from vault.tee.metadata import InstanceMetadata
from vault.tee.sealing import NoopSealer, SealError, Sealer
from vault.templates import put_template
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    live_create_request,
    make_employer_template,
    new_id,
    put_candidate_and_employer_templates,
    sample_package,
)

# --- 目印(グリッド上の値で、ほかのどこにも現れない組み合わせ) ---

BANDS = CandidateAttributeBands(
    experience_band="10y_plus", region_block="kyushu_okinawa", job_category="medical_welfare"
)
# 候補者のポリシーに入れる受けるアンカー。night_duty=8 は、候補者にとって最も悪い値(外した軸 night_duty に中立)
BASE_ANCHOR = Anchor(
    salary=1450,
    remote_days=5,
    night_duty=8,
    review_months=12,
    training="none",
    side_job="not_allowed",
    start="within_6_months",
)
# 途中確認で聞く組み合わせ(回答のカナリア)。BASE_ANCHOR より年収が低いので、候補者には「本人確認が必要」。night_duty=8 なので、
# 「受ける」の回答は外した軸に中立で、依頼者の本体にも追記される(§2.4・§4.4)
ASKED = Package(
    salary=1350,
    remote_days=1,
    night_duty=8,
    review_months=12,
    training="available",
    side_job="not_allowed",
    start="within_6_months",
)
# 求人側の依頼者のポリシーの受けないアンカー。ASKED は含まれないので、求人側にも「本人確認が必要」
EMPLOYER_REJECT = Anchor(
    salary=300,
    remote_days=0,
    night_duty=8,
    review_months=12,
    training="none",
    side_job="allowed",
    start="within_1_month",
)
BLOCKED_COMPANY = "canary-company-9f3b1e7a"
CANARY_STRINGS = (
    '"within_6_months"',
    '"not_allowed"',
    '"night_duty"',  # 外した軸
    '"10y_plus"',
    '"kyushu_okinawa"',
    '"medical_welfare"',
    f'"{BLOCKED_COMPANY}"',
)
CANARY_NUMBERS = (1350, 1450)  # ASKED と BASE_ANCHOR の年収。カウンタ・版・番号には現れない大きさ

# --- §9 の 2「何を暗号化するか」(DV-19 の期待値)。文書の中の項目の位置で書く(participants・views は 1 段下の項目まで) ---

_SIDES = ("candidate", "employer")
_LISTED_PARTICIPANT_ITEMS = ("principal_id", "template_id", "job_id", "is_fictional")  # 依頼者 ID・テンプレート ID・job_id・架空かどうか
SEALED = {
    "principal": {"policy", "blocklist", "removed_axes", "attribute_bands"},
    "negotiation:live": {
        "snapshots",
        "pending_offer",
        "last_check",
        "pending_question",
        "result",
        "participants.candidate.attribute_bands",
    },
    "event:live": {"views.candidate.payload", "views.employer.payload"},
}
# 平文のまま残る項目のうち、§9 の 2 の列挙にあるもの(索引・制御に使う項目)
PLAINTEXT_LISTED = {
    "principal": {"evaluation_budget"},  # カウンタ
    "negotiation:live": {
        "status",
        "end_reason",
        "to_move",
        "paused",
        "paused_at",
        "expires_at",
        "deadline",  # 期限
        "version",
        "seq",
        "mode",
        "request_id",
        "ttl_at",
        "counters",
        *(f"participants.{side}.{name}" for side in _SIDES for name in _LISTED_PARTICIPANT_ITEMS),
    },
    "event:live": {"version", "ttl_at", "views.candidate.seq", "views.employer.seq"},
    "idempotency": {"created_at", "ttl_at"},  # request_id のハッシュが文書 ID。ttl_at はデモ・攻撃のキーだけ
}
# 列挙にはないが、平文のまま残す項目(文書 ID・時刻・公開フィクスチャの情報・削除の印)。設計書の列挙に足す候補
PLAINTEXT_UNLISTED = {
    "principal": set(),
    "negotiation:live": {
        "nid",
        "created_at",
        *(f"participants.{side}.{name}" for side in _SIDES for name in ("company_id", "job_category_info")),
        "participants.employer.attribute_bands",  # 求人側には帯がない(null)。候補者の帯だけが封印の対象
    },
    "event:live": set(),
    "idempotency": {"nid"},
}
# 試験の場面では現れないことがある平文の項目(deleting は、本人の削除の途中にだけ立つ)
PLAINTEXT_OPTIONAL = {"principal": {"deleting"}}

PRIVATE_KINDS = ("principal", "negotiation:live", "event:live", "idempotency")  # カナリアを探す文書(デモ・攻撃・テンプレートは公開)
PUBLIC_KINDS = ("negotiation:demo", "negotiation:attack", "event:demo", "event:attack", "template")


# --- vault-db の全文書の全探索 ---


@dataclass(frozen=True)
class Doc:
    path: str
    kind: str
    data: dict

    @property
    def items(self) -> dict:
        """項目の位置 → 値。participants・views は 1 段下の項目まで開く(その側に見えない記録 views.<側>=null は、項目としない)。"""
        out = {}
        for name, value in self.data.items():
            if name in ("participants", "views"):
                for side, sub in value.items():
                    if sub is not None:
                        out.update({f"{name}.{side}.{key}": item for key, item in sub.items()})
            else:
                out[name] = value
        return out


def _all_documents(db) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []

    def walk(collections) -> None:
        for collection in collections:
            for snap in collection.stream():
                found.append((snap.reference.path, snap.to_dict()))
                walk(snap.reference.collections())  # events のようなサブコレクション

    walk(db.collections())
    return found


def dump(db) -> list[Doc]:
    raw = _all_documents(db)
    modes = {path: data["mode"] for path, data in raw if path.startswith("negotiations/") and path.count("/") == 1}
    plain_kinds = {"principals": "principal", "idempotency": "idempotency", "templates": "template"}
    docs = []
    for path, data in raw:
        parts = path.split("/")
        if parts[0] == "negotiations" and len(parts) == 2:
            kind = f"negotiation:{data['mode']}"
        elif parts[0] == "negotiations" and len(parts) == 4 and parts[2] == "events":
            kind = f"event:{modes['/'.join(parts[:2])]}"
        elif parts[0] in plain_kinds and len(parts) == 2:
            kind = plain_kinds[parts[0]]
        else:
            raise AssertionError(f"想定外の文書がある(§9 の 2 の列挙にない): {parts[0]}")
        docs.append(Doc(path, kind, data))
    return docs


# --- 平文の部分の検索 ---


def _without_bytes(value):
    if isinstance(value, bytes):
        return None
    if isinstance(value, dict):
        return {key: _without_bytes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_bytes(item) for item in value]
    return value


def _all_values(value):
    """bytes を含む、すべての葉の値。"""
    if isinstance(value, dict):
        for item in value.values():
            yield from _all_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_values(item)
    else:
        yield value


def _plain_leaves(value):
    """bytes を除く、すべての葉の値。"""
    if isinstance(value, dict):
        for item in value.values():
            yield from _plain_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _plain_leaves(item)
    elif not isinstance(value, bytes):
        yield value


def _key_paths(value, prefix: str = ""):
    """bytes を除く、dict のキーの位置(. 区切り)。"""
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{prefix}{key}"
            yield from _key_paths(item, f"{prefix}{key}.")
    elif isinstance(value, list):
        for item in value:
            yield from _key_paths(item, prefix)


def canaries_found(docs: list[Doc]) -> set[str]:
    """private な文書の、bytes 以外の値を JSON にした文字列(と、数の葉・軸と帯のキー名)に現れる目印。"""
    found: set[str] = set()
    forbidden_keys = set(AXIS_KEYS) | set(ATTRIBUTE_BANDS)
    for doc in docs:
        if doc.kind not in PRIVATE_KINDS:
            continue
        text = json.dumps(_without_bytes(doc.data), default=str, ensure_ascii=False)
        found.update(canary for canary in CANARY_STRINGS if canary in text)
        found.update(f"number:{number}" for number in CANARY_NUMBERS if number in list(_plain_leaves(doc.data)))
        # 軸と帯の名前をキーに持つ平文の dict(ポリシー・組み合わせ・帯)があれば、封印の漏れ。求人の公開情報 job_category だけは平文
        keys = {path for path in _key_paths(doc.data) if path.rsplit(".", 1)[-1] in forbidden_keys}
        found.update(f"key:{path}" for path in keys - {"participants.employer.job_category_info.job_category"})
    return found


def sealed_violations(doc: Doc) -> list[str]:
    """封印の対象なのに bytes でない項目(なければ空)。principals は書かれた項目だけ、イベントは見える側だけ見る。"""
    expected = SEALED[doc.kind]
    items = doc.items
    if doc.kind == "principal":
        expected = expected & items.keys()
    elif doc.kind == "event:live":
        expected = {f"views.{side}.payload" for side in _SIDES if f"views.{side}.seq" in items}
    return sorted(path for path in expected if not isinstance(items.get(path), bytes))


def unexpected_bytes(doc: Doc) -> set[str]:
    return {path for path, value in doc.items.items() if isinstance(value, bytes)} - SEALED[doc.kind]


# --- 場面 ---


def write_live_negotiation_between(
    store: VaultStore, layer: SealLayer, clock: FixedClock, cand_pid: str, emp_pid: str
) -> str:
    """相手(求人側)も本物の依頼者である live の交渉の文書を、封印レイヤ経由で置く(と、その冪等キー)。ポリシーは両者の本体の写し。"""
    nid, now, request_id = generate_id(), clock.now(), f"{cand_pid}:request-b"
    doc = NegotiationDocument(
        nid=nid,
        status="active",
        to_move="candidate",
        created_at=now,
        expires_at=now + dt.timedelta(hours=72),
        deadline=now + dt.timedelta(minutes=5),
        snapshots=Snapshots(candidate=store.get_policy(cand_pid).policy, employer=store.get_policy(emp_pid).policy),
        participants=Participants(
            candidate=Participant(is_fictional=False, principal_id=cand_pid, attribute_bands=BANDS),
            employer=Participant(is_fictional=False, principal_id=emp_pid, job_id="job-b", company_id="company-b"),
        ),
        request_id=request_id,
        mode="live",
    )
    ref = store._negotiation_ref(nid)
    ref.set(layer.negotiation_to_firestore(ref.path, doc))
    store._idempotency_ref(request_id).set({"nid": nid, "created_at": now})
    return nid


def _move(store, nid, version, side, move, package=None) -> int:
    response = store.process_move(nid, MoveRequest(expected_version=version, side=side, move=move, package=package))
    assert response.valid is True
    return response.version


def _accept(store, nid, version) -> str | None:
    """求人側が提案を受ける(accept)。終わった理由を返す。"""
    return store.process_move(nid, MoveRequest(expected_version=version, side="employer", move="accept")).end_reason


def _opened(sealer: Sealer, path: str, field: str, stored: bytes):
    """封印した項目を開けて、JSON から戻す(AAD の「文書のパス + 項目名」は、試験が自分で与える)。"""
    return json.loads(sealer.open(path, field, stored))


def _answer(store, nid, version, side, answer) -> int:
    return store.process_principal_answer(
        nid, PrincipalAnswerRequest(expected_version=version, side=side, package=ASKED, answer=answer)
    ).version


@dataclass
class Scenario:
    cand_pid: str
    emp_pid: str
    nid_a: str
    nid_b: str
    dumps: list[list[Doc]]  # 途中確認の待ち・提案の保留・判定の後、のそれぞれの時点での vault-db の全文書

    @property
    def docs(self) -> list[Doc]:
        return [doc for dump_ in self.dumps for doc in dump_]


def run_scenario(store: VaultStore, layer: SealLayer, clock: FixedClock) -> Scenario:
    """live の依頼者 2 人(候補者・求人)で、交渉を 2 つ進めて途中確認に答える。デモと攻撃の交渉も 1 つずつ置く。"""
    db = store._db
    dumps: list[list[Doc]] = []
    cand_pid, emp_pid = new_id("principal"), new_id("principal")
    employer_template = make_employer_template()
    put_template(db, employer_template)
    candidate_policy = Policy(side="candidate", accept_anchors=[BASE_ANCHOR])
    store.put_policy(
        cand_pid, PutPolicyRequest(policy=candidate_policy, removed_axes=["night_duty"], attribute_bands=BANDS)
    )
    store.put_blocklist(cand_pid, PutBlocklistRequest(blocklist=[BLOCKED_COMPANY]))
    store.put_policy(emp_pid, PutPolicyRequest(policy=Policy(side="employer", reject_anchors=[EMPLOYER_REJECT])))

    # A: 本物の候補者 対 架空の求人。候補者が聞いて、答えて、提案し、求人が受ける。
    created = store.create_negotiation(live_create_request(cand_pid, employer_template.template_id))
    assert created.status == "created"
    nid_a = created.nid
    v = _move(store, nid_a, 0, "candidate", "ask_principal", ASKED)
    dumps.append(dump(db))  # 候補者の途中確認の待ち(pending_question)
    v = _answer(store, nid_a, v, "candidate", "accept")
    v = _move(store, nid_a, v, "candidate", "propose", ASKED)
    dumps.append(dump(db))  # 提案の保留(pending_offer・last_check)
    assert _accept(store, nid_a, v) == "agreed"
    dumps.append(dump(db))  # 判定の後(result)

    # B: 求人側も本物の依頼者。候補者の提案(A の回答で受けられる)に、求人側が聞いて、答えて、受ける。
    nid_b = write_live_negotiation_between(store, layer, clock, cand_pid, emp_pid)
    v = _move(store, nid_b, 0, "candidate", "propose", ASKED)
    v = _move(store, nid_b, v, "employer", "ask_principal", ASKED)
    dumps.append(dump(db))  # 求人側の依頼者の途中確認の待ち
    v = _answer(store, nid_b, v, "employer", "accept")
    assert _accept(store, nid_b, v) == "agreed"
    dumps.append(dump(db))

    # デモと攻撃(架空人物どうし)。封印しない
    candidate_template, demo_employer_template = put_candidate_and_employer_templates(db)
    for mode in ("demo", "attack"):
        nid = store.create_negotiation(
            demo_create_request(candidate_template.template_id, demo_employer_template.template_id, mode=mode)
        ).nid
        _move(store, nid, 0, "candidate", "check", sample_package())
    dumps.append(dump(db))
    return Scenario(cand_pid, emp_pid, nid_a, nid_b, dumps)


# --- フィクスチャ ---


@pytest.fixture
def sealer() -> Sealer:
    return Sealer(os.urandom(32))


@pytest.fixture
def sealed_store(firestore_client, clock, sealer) -> VaultStore:
    return VaultStore(db=firestore_client, clock=clock, config=DEFAULT_VAULT_CONFIG, sealer=sealer)


@pytest.fixture
def sealed_run(sealed_store, sealer, clock) -> Scenario:
    return run_scenario(sealed_store, SealLayer(sealer), clock)


# --- (a)(b)(c) 本物の Sealer ---


def test_every_sealed_item_is_bytes_in_every_document(sealed_run):
    checked = set()
    for doc in sealed_run.docs:
        if doc.kind in SEALED:
            assert sealed_violations(doc) == [], f"平文の項目がある: {doc.kind}"
            assert unexpected_bytes(doc) == set(), f"封印の対象でない項目が bytes: {doc.kind}"
            checked.add(doc.kind)
    assert checked == set(SEALED)  # 3 種類の文書(依頼者・live の交渉・live のイベント)をすべて見た


def test_the_plaintext_items_are_exactly_the_ones_in_section_9(sealed_run):
    found: dict[str, set[str]] = {}
    for doc in sealed_run.docs:
        if doc.kind in PLAINTEXT_LISTED:
            plain = {path for path, value in doc.items.items() if not isinstance(value, bytes)}
            found.setdefault(doc.kind, set()).update(plain)

    assert found.keys() == PLAINTEXT_LISTED.keys()
    for kind, listed in PLAINTEXT_LISTED.items():
        expected = listed | PLAINTEXT_UNLISTED[kind]
        optional = PLAINTEXT_OPTIONAL.get(kind, set())
        extra, missing = found[kind] - optional - expected, expected - found[kind]
        assert not extra and not missing, f"{kind}: 列挙にない平文 {extra}、足りない {missing}"


def test_the_canaries_appear_nowhere_in_plaintext(sealed_run):
    assert canaries_found(sealed_run.docs) == set()


def test_demo_attack_and_template_documents_are_not_sealed(sealed_run):
    public = [doc for doc in sealed_run.dumps[-1] if doc.kind in PUBLIC_KINDS]

    assert {doc.kind for doc in public} == set(PUBLIC_KINDS)  # デモ・攻撃の交渉とそのイベント、テンプレートがある
    for doc in public:
        assert not any(isinstance(leaf, bytes) for leaf in _all_values(doc.data)), doc.kind


# --- (d) NoopSealer: 保存の形は変わらず、(a) が成り立たない ---


def test_with_the_noop_sealer_the_items_are_plaintext_and_the_canaries_are_found(store, clock):
    run = run_scenario(store, SealLayer(NoopSealer()), clock)

    for kind in SEALED:  # 3 種類とも、封印の対象の項目が bytes でない(試験は本当に封印を見ている)
        docs = [doc for doc in run.docs if doc.kind == kind]
        assert docs and all(sealed_violations(doc) for doc in docs), kind
    assert not any(isinstance(leaf, bytes) for doc in run.docs for leaf in _all_values(doc.data))
    found = canaries_found(run.docs)
    assert set(CANARY_STRINGS) <= found and {f"number:{number}" for number in CANARY_NUMBERS} <= found  # 対照: 目印は平文で見つかる
    assert {"key:snapshots.candidate.accept_anchors.salary", "key:policy.accept_anchors.salary"} <= found


# --- (e) 別の DEK では開けない ---


def test_documents_sealed_with_one_dek_cannot_be_read_with_another(sealed_run, sealer, firestore_client, clock):
    other = VaultStore(db=firestore_client, clock=clock, config=DEFAULT_VAULT_CONFIG, sealer=Sealer(os.urandom(32)))

    with pytest.raises(SealError):
        other.get_policy(sealed_run.cand_pid)
    with pytest.raises(SealError):
        other.get_view(sealed_run.nid_a, "candidate")
    with pytest.raises(SealError):
        other.get_events(sealed_run.nid_a, "candidate")
    with pytest.raises(SealError):
        other.list_principal_negotiations(sealed_run.cand_pid)
    # 封印した本人の DEK のストアでは、同じ文書がすべて読める(対照)
    own = VaultStore(db=firestore_client, clock=clock, config=DEFAULT_VAULT_CONFIG, sealer=sealer)
    assert own.get_policy(sealed_run.cand_pid).attribute_bands == BANDS
    assert [e.kind for e in own.get_events(sealed_run.nid_a, "candidate")][:1] == ["ask_principal"]


# --- AAD は「文書のパス + 項目名」 ---


def test_the_aad_is_the_document_path_and_the_item_name(sealed_run, sealer, sealed_store):
    cand = sealed_store._principal_ref(sealed_run.cand_pid).get().to_dict()
    path = f"principals/{sealed_run.cand_pid}"
    expected_policy = sealed_store.get_policy(sealed_run.cand_pid).policy.model_dump(mode="json")
    assert _opened(sealer, path, "policy", cand["policy"]) == expected_policy
    assert _opened(sealer, path, "attribute_bands", cand["attribute_bands"]) == BANDS.model_dump(mode="json")
    assert _opened(sealer, path, "removed_axes", cand["removed_axes"]) == ["night_duty"]
    assert _opened(sealer, path, "blocklist", cand["blocklist"]) == [BLOCKED_COMPANY]
    with pytest.raises(SealError):  # 項目名が違えば開かない
        sealer.open(path, "blocklist", cand["policy"])
    with pytest.raises(SealError):  # 文書のパスが違えば開かない
        sealer.open(f"principals/{sealed_run.emp_pid}", "policy", cand["policy"])

    path = f"negotiations/{sealed_run.nid_a}"
    negotiation = sealed_store._negotiation_ref(sealed_run.nid_a).get().to_dict()
    assert _opened(sealer, path, "result", negotiation["result"])["package"] == ASKED.model_dump(mode="json")
    assert _opened(sealer, path, "snapshots", negotiation["snapshots"]) is None  # 判定で消えた(null も封印してある)
    bands_field = "participants.candidate.attribute_bands"
    sealed_bands = negotiation["participants"]["candidate"]["attribute_bands"]
    assert _opened(sealer, path, bands_field, sealed_bands) == BANDS.model_dump(mode="json")

    event = sealed_store._events(sealed_run.nid_a).document("00000001").get().to_dict()  # 候補者の ask_principal
    sealed_payload = event["views"]["candidate"]["payload"]
    payload = _opened(sealer, f"{path}/events/00000001", "views.candidate.payload", sealed_payload)
    assert (payload["kind"], payload["package"]) == ("ask_principal", ASKED.model_dump(mode="json"))
    assert event["views"]["candidate"]["seq"] == 1 and event["views"]["employer"] is None  # seq は平文。見えない側は null


# --- 暗号文の移し替え・平文への書き換えは読めない ---


def test_a_ciphertext_moved_to_another_document_or_item_cannot_be_read(sealed_run, sealed_store):
    cand = sealed_store._principal_ref(sealed_run.cand_pid).get().to_dict()
    sealed_store._principal_ref(sealed_run.emp_pid).update({"policy": cand["policy"]})  # 別の依頼者の文書へ
    with pytest.raises(SealError):
        sealed_store.get_policy(sealed_run.emp_pid)

    negotiation = sealed_store._negotiation_ref(sealed_run.nid_b).get().to_dict()
    sealed_store._negotiation_ref(sealed_run.nid_b).update({"pending_offer": negotiation["last_check"]})  # 同じ文書の別の項目へ
    with pytest.raises(SealError):
        sealed_store.get_view(sealed_run.nid_b, "employer")

    first, second = (sealed_store._events(sealed_run.nid_a).document(f"{n:08d}") for n in (1, 2))
    payloads = [ref.get().to_dict()["views"]["candidate"]["payload"] for ref in (first, second)]
    first.update({"views.candidate.payload": payloads[1]})  # 別の記録へ
    with pytest.raises(SealError):
        sealed_store.get_events(sealed_run.nid_a, "candidate")


def test_plaintext_written_into_a_sealed_item_is_refused(sealed_run, sealed_store):
    plain_policy = accept_all_policy("candidate").model_dump(mode="json")

    sealed_store._principal_ref(sealed_run.cand_pid).update({"policy": plain_policy})
    with pytest.raises(SealError):
        sealed_store.get_policy(sealed_run.cand_pid)

    snapshots = {"candidate": plain_policy, "employer": plain_policy}
    sealed_store._negotiation_ref(sealed_run.nid_b).update({"snapshots": snapshots})
    with pytest.raises(SealError):
        sealed_store.get_view(sealed_run.nid_b, "candidate")

    event_ref = sealed_store._events(sealed_run.nid_a).document("00000001")
    flat_view = {"seq": 1, "kind": "check", "package": ASKED.model_dump(mode="json")}  # 封印する前の形
    event_ref.update({"views.candidate": flat_view})
    with pytest.raises(SealError):
        sealed_store.get_events(sealed_run.nid_a, "candidate")


# --- トランザクションの再試行 ---


def test_a_retried_transaction_seals_again_with_a_fresh_nonce_and_the_same_content(
    sealed_store, sealer, clock, monkeypatch
):
    pid = new_id("principal")
    employer_template = make_employer_template()
    put_template(sealed_store._db, employer_template)
    sealed_store.put_policy(
        pid, PutPolicyRequest(policy=accept_all_policy("candidate"), attribute_bands=BANDS)
    )
    nid = sealed_store.create_negotiation(live_create_request(pid, employer_template.template_id)).nid
    writes: list[tuple[str, dict]] = []
    commits: list[int] = []
    real_set, real_commit = Transaction.set, Transaction._commit

    def recording_set(self, reference, document_data, *args, **kwargs):
        writes.append((reference.path, document_data))
        return real_set(self, reference, document_data, *args, **kwargs)

    def abort_the_first_commit(self):
        commits.append(1)
        if len(commits) == 1:
            self._rollback()  # 本物の Firestore は、競合したトランザクションを自分で終わらせてから Aborted を返す
            raise Aborted("simulated contention")  # 書き込みを積んだあとの競合 → 同じ関数がもう一度走る
        return real_commit(self)

    monkeypatch.setattr(Transaction, "set", recording_set)
    monkeypatch.setattr(Transaction, "_commit", abort_the_first_commit)

    sealed_store.process_move(nid, MoveRequest(expected_version=0, side="candidate", move="check", package=ASKED))

    assert len(commits) == 2
    for path, fields in (
        (f"negotiations/{nid}", ("snapshots", "last_check", "pending_offer", "result", "pending_question")),
        (f"negotiations/{nid}/events/00000001", ("views.candidate.payload",)),
    ):
        first, second = [data for written_path, data in writes if written_path == path]
        for field in fields:
            sealed_pair = [_dig(data, field) for data in (first, second)]
            assert sealed_pair[0] != sealed_pair[1]  # nonce は毎回新しい
            assert sealer.open(path, field, sealed_pair[0]) == sealer.open(path, field, sealed_pair[1])  # 中身は同じ
    view = sealed_store.get_view(nid, "candidate")  # 保存されたのは 2 回目で、読み戻せる
    assert view.last_check.package == ASKED and view.version == 1


def _dig(data: dict, field: str):
    for part in field.split("."):
        data = data[part]
    return data


# --- 本人の削除 ---


def test_deleting_a_principal_works_on_sealed_documents_and_keeps_the_counterparts_view(
    sealed_store, sealer, clock
):
    cand_pid, emp_pid = new_id("principal"), new_id("principal")
    sealed_store.put_policy(cand_pid, PutPolicyRequest(policy=accept_all_policy("candidate"), attribute_bands=BANDS))
    sealed_store.put_policy(emp_pid, PutPolicyRequest(policy=accept_all_policy("employer")))
    nid = write_live_negotiation_between(sealed_store, SealLayer(sealer), clock, cand_pid, emp_pid)
    v = _move(sealed_store, nid, 0, "candidate", "check", ASKED)
    v = _move(sealed_store, nid, v, "candidate", "propose", ASKED)
    _move(sealed_store, nid, v, "employer", "check", ASKED)

    sealed_store.delete_principal(cand_pid)

    path, raw = f"negotiations/{nid}", sealed_store._negotiation_ref(nid).get().to_dict()
    candidate = raw["participants"]["candidate"]
    assert candidate["principal_id"] is None  # 依頼者 ID は最後に消える
    assert _opened(sealer, path, "participants.candidate.attribute_bands", candidate["attribute_bands"]) is None
    last_check = _opened(sealer, path, "last_check", raw["last_check"])
    assert last_check["candidate"] is None and last_check["employer"]["package"] == ASKED.model_dump(mode="json")
    assert _opened(sealer, path, "pending_offer", raw["pending_offer"]) is None  # 未決の提案も消えた
    assert sealed_store.get_view(nid, "employer").last_check.package == ASKED  # 相手は、封印したまま自分の見え方を読める
    assert [e.kind for e in sealed_store.get_events(nid, "employer")] == ["offer_received", "check", "final_result"]
    assert all(doc.to_dict()["views"]["candidate"] is None for doc in sealed_store._events(nid).stream())
    assert not sealed_store._principal_ref(cand_pid).get().exists


def test_deleting_the_principal_who_was_asked_erases_the_pending_question_in_the_sealed_document(
    sealed_store, sealer, clock
):
    cand_pid, emp_pid = new_id("principal"), new_id("principal")
    no_anchors = Policy(side="candidate")  # アンカーがないので、ASKED は「本人確認が必要」
    sealed_store.put_policy(cand_pid, PutPolicyRequest(policy=no_anchors, attribute_bands=BANDS))
    sealed_store.put_policy(emp_pid, PutPolicyRequest(policy=accept_all_policy("employer")))
    nid = write_live_negotiation_between(sealed_store, SealLayer(sealer), clock, cand_pid, emp_pid)
    _move(sealed_store, nid, 0, "candidate", "ask_principal", ASKED)
    path = f"negotiations/{nid}"
    before = sealed_store._negotiation_ref(nid).get().to_dict()
    assert _opened(sealer, path, "pending_question", before["pending_question"])["side"] == "candidate"

    sealed_store.delete_principal(cand_pid)

    after = sealed_store._negotiation_ref(nid).get().to_dict()
    assert _opened(sealer, path, "pending_question", after["pending_question"]) is None  # 聞かれていた側の質問は消えた


# --- Cloud Run 版・TEE の起動口 ---


def test_the_cloud_run_entry_point_does_not_seal(monkeypatch, firestore_client):
    monkeypatch.setattr("vault.app._create_vault_db", lambda: firestore_client)
    client = TestClient(create_app_from_env())
    pid, policy = new_id("principal"), accept_all_policy("candidate")

    body = {"policy": policy.model_dump(mode="json"), "attribute_bands": BANDS.model_dump(mode="json")}
    response = client.put(f"/v1/principals/{pid}/policy", json=body)

    assert response.status_code == 204
    stored = firestore_client.document(f"principals/{pid}").get().to_dict()
    assert stored["policy"] == policy.model_dump(mode="json")  # 保存の形は、封印のない版と同じ(平文の dict)
    assert stored["attribute_bands"] == BANDS.model_dump(mode="json")



def test_the_tee_entry_point_gives_the_store_the_sealer_made_from_the_dek(monkeypatch, firestore_client):
    dek, built = os.urandom(32), {}
    metadata = InstanceMetadata(
        project_id="demo-project",
        project_number="123456789012",
        zone="asia-northeast1-b",
        region="asia-northeast1",
        instance_name="vault-1",
    )
    monkeypatch.setattr(main_module.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(main_module, "load_vault_tee_config", lambda: object())
    monkeypatch.setattr(main_module, "read_instance_metadata", lambda: metadata)
    monkeypatch.setattr(main_module, "create_client", lambda project=None: firestore_client)
    monkeypatch.setattr(main_module, "release_dek", lambda db, *, metadata, config: dek)
    monkeypatch.setattr(main_module, "prepare_tls", lambda config: (Path("key.pem"), Path("cert.pem"), "0" * 64))
    monkeypatch.setattr(main_module, "build_app", lambda store, *args: built.update(store=store) or FastAPI())
    monkeypatch.setattr(main_module, "serve", lambda app, config, key_path, cert_path: None)

    assert main_module.main() == 0

    pid = new_id("principal")
    built["store"].put_policy(pid, PutPolicyRequest(policy=accept_all_policy("candidate"), attribute_bands=BANDS))
    stored = firestore_client.document(f"principals/{pid}").get().to_dict()
    assert isinstance(stored["policy"], bytes)  # 起動口が組んだストアは、封印して書く
    opened = _opened(Sealer(dek), f"principals/{pid}", "policy", stored["policy"])  # 起動口に渡した DEK で開く
    assert opened == accept_all_policy("candidate").model_dump(mode="json")
