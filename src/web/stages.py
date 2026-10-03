"""段階開示の状態 stages/{nid}(design.md §6.2)と、段の遷移・架空の求人の自動応答。`web` の (default) の Firestore に持つ。

【段の状態の作成・TTL・削除】(1d-1・1d-2)
- 本人の削除と 30 日の自動削除(§6.3)が、本人の段の状態を消す口(delete_for_principal)。
- デモ・攻撃の段の状態に、金庫と同じ 96 時間の期限の項目(ttl_at)を付ける(台帳 I-6)。
- デモ用のエンドポイントが、架空の候補者の交渉かを確かめる口(is_fictional_negotiation)。
- 交渉ごとの LLM の物理の呼び出し数(llm_calls。web.llm_budget が、送る前のトランザクションで進める。§8.2)も、この文書の項目
  として持つ(TTL と削除は段の状態と同じ。台帳 L13-5)。作成のときは 0 から始める。
stages/{nid} は、交渉の作成直後に作る。作り損ねても、見回り(§4.1)と、画面で交渉を開いたときに、なければ作る(冪等。台帳 L9-4)。
そのため、判定の後に web が落ちても、段階開示の状態は失われない(DV-08)。本物の候補者の依頼者 ID を持たせるのは、削除のときに
これで引くため(金庫の削除が先に済んで、金庫から交渉が消えていても見つけられるように。§6.2)。候補者が架空人物(デモ・攻撃)なら None。
期限の項目 ttl_at は、候補者が架空人物のものにだけ付ける(本物の利用者の段の状態は、本人の削除と 30 日の自動削除で消える。TTL では
消さない)。Firestore の TTL ポリシー自体の設定はデプロイの段で行う。

【段の遷移】(④。§6.2)
- 段 0(自動): 見込みと組み合わせを双方に出す。中身はイベント列の最終記録で、web には写さない。合意で終わった交渉だけが、段 1 以降に進める
  (見込み「なし」は、段 0 の表示で終わり)。判定を見つけたとき(agreed_at)に、段 0 を台帳に記録する。
- 段 1: 双方が「会う」を押した時点で、候補者の匿名職務要約を求人側に出す(非公開求人なら、企業名を候補者に出す。FR-32)。要約は
  候補者本人が書く(本物の候補者は「会う」のときに送る。デモ・攻撃の架空の候補者はフィクスチャのもの)。LLM は通さない(FR-33)。
- 段 2: 双方が「承認」を押した時点で、氏名と連絡先を出す。実ユーザーの段 2 は模擬表示で、連絡先を集めない(P-2)。
「会う」「承認」は、側ごとのフラグとして冪等に立てる(press)。両方のフラグがそろったトランザクションの中でだけ、次の段へ進める。同じ
トランザクションで、台帳(web.ledger)に出来事を書く。承認は、段 1 が開いてから(段 0 では押せない)。

【架空人物の自動応答】(P-2)
求人側は、ハッカソンではいつもフィクスチャで、人がいない。フィクスチャの設定(auto_response)に従って、サーバが「会う」「承認」を自動で
押す(StageFlow)。デモ・攻撃の架空の候補者も、フィクスチャの職務要約・連絡先で、サーバが自動で押す。ほかの訪問者が求人側を操作する経路は
作らない(API は候補者側の操作だけ)。自動応答は、段の状態を読んだとき・候補者が操作したときに、その場で冪等に行う(判定の後に web が
落ちても、次に開いたときに行われる)。

ログには、競合で書けなかったこと(StageBusy)だけを書く(依頼者 ID・交渉 ID・職務要約は書かない)。
"""

import asyncio
import dataclasses
import datetime as dt
import logging
import random
import re
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from google.api_core.exceptions import Aborted, AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field

from negotiation_core import ID_PATTERN, CandidateAttributeBands, Package, Side

from vault.api_models import EventViewItem, PrincipalNegotiationSummary
from vault.clock import Clock
from vault.fixtures import CandidateFixture, EmployerFixture
from vault.models import Likelihood, NegotiationResult

from web.config import DEFAULT_WEB_CONFIG, RetentionConfig
from web.fictional_answerer import FixtureCatalog
from web.ledger import DisclosureLedger, LedgerOperator, LedgerRecipient, LedgerRow
from web.vault_client import VaultClient, VaultNotFoundError

_log = logging.getLogger(__name__)

STAGES_COLLECTION = "stages"

_NID_RE = re.compile(ID_PATTERN)
# 1 回のバッチで消す文書の数(Firestore のバッチの上限 500 より小さく)。
_DELETE_BATCH_SIZE = 300

# 段の番号。0 は見込みと組み合わせ、1 は匿名職務要約、2 は氏名と連絡先。
LAST_STAGE = 2

# トランザクションの再試行は「内側 5 回(google-cloud-firestore の max_attempts)× 外側 6 回」。同じ交渉の「会う」「承認」が並行・
# 再送で重なると、同じ文書を読んだ呼び出しが競合する。本番の Firestore は内側の再試行で順番を保つが、エミュレータでは、一斉に中止され、
# 一斉にやり直して、また中止される。そのため、内側を使い切ったら、乱数の待ちを入れて、新しいトランザクションでやり直す(web.llm_budget と
# 同じ考え方)。それでも書けなければ StageBusy(呼び出し側は 503 にする。操作は冪等なので、呼び直してよい)。
_INNER_MAX_ATTEMPTS = 5
_OUTER_ATTEMPTS = 6
_OUTER_BACKOFF_SECONDS = (0.010, 0.200)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.toml"


@dataclass(frozen=True)
class StagesConfig:
    """段階開示の暫定値(config/params.toml の [web.stages]。§6.2・台帳 C-35)。"""

    job_summary_max_chars: int
    max_request_body_bytes: int


def load_stages_config(path: Path = _CONFIG_PATH) -> StagesConfig:
    """config/params.toml から [web.stages] を読み込む。"""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    try:
        section = raw["web"]["stages"]
        config = StagesConfig(
            job_summary_max_chars=int(section["job_summary_max_chars"]),
            max_request_body_bytes=int(section["max_request_body_bytes"]),
        )
    except KeyError as exc:
        raise ValueError(f"{path} is missing a required [web.stages] key: {exc}") from exc
    if config.job_summary_max_chars < 1 or config.max_request_body_bytes < 1:
        raise ValueError(f"{path}: web.stages limits must be positive")
    return config


DEFAULT_STAGES_CONFIG: StagesConfig = load_stages_config()

# 段ごとに見えるもの(§6.2)。これが、相手に見せてよいものの正本の表。それ以外は見せない。
Item = Literal["likelihood", "package", "job_summary", "name", "email"]

# 段 n が開いたときに、新しく見せるもの(開示台帳の行になる)。
OPENED_BY_STAGE: dict[int, Sequence[Item]] = dict(enumerate([("likelihood", "package"), ("job_summary",), ("name", "email")]))
# 段 n が開いたときに、見せる相手。段 0 は双方、段 1・2 は候補者の情報を求人側へ。
RECIPIENT_BY_STAGE: dict[int, LedgerRecipient] = dict(enumerate(["both", "employer", "employer"]))
# 段 n が開いているとき、求人側に見えるもの(累積)。段 0 は見込みと組み合わせ、段 1 は匿名職務要約、段 2 は氏名と連絡先。
EMPLOYER_SEES: dict[int, Sequence[Item]] = dict(
    (stage, tuple(item for opened in range(stage + 1) for item in OPENED_BY_STAGE[opened])) for stage in range(LAST_STAGE + 1)
)
# 合意に至らなかった交渉(見込み「なし」)で、求人側に見えるもの(FR-28: 「なし」だけ。組み合わせは出さない)。
EMPLOYER_SEES_WITHOUT_AGREEMENT: Sequence[Item] = ("likelihood",)
# 非公開求人(confidential)の企業名を候補者に出す段(FR-32)。公開の求人は最初から出す。
COMPANY_NAME_STAGE = 1


StageKind = Literal["meet", "approve"]
PressRefusal = Literal["absent", "not_agreed", "stage_not_open"]


def _both_sides_off() -> dict[str, bool]:
    return dict(candidate=False, employer=False)


class StageDocument(BaseModel):
    """stages/{nid} 文書の形。stage=0 は段 0(見込みと組み合わせを双方に自動で表示する段)。"""

    model_config = ConfigDict(extra="forbid")

    nid: str
    candidate_principal_id: str | None
    stage: int = 0
    created_at: dt.datetime
    ttl_at: dt.datetime | None = None  # デモ・攻撃(候補者が架空人物)にだけ付ける(台帳 I-6)
    llm_calls: int = 0  # この交渉の、LLM への物理の呼び出し数(§8.2。web.llm_budget が進める)
    # デモ・攻撃の交渉の、フィクスチャのテンプレート ID(自動応答がフィクスチャを引く元。作成の呼び出し側が分かる場合だけ)。
    # 本物の候補者の交渉の求人は、金庫の交渉の job_id から引く(§6.2)ので、持たない。
    employer_template_id: str | None = None
    candidate_template_id: str | None = None
    agreed_at: dt.datetime | None = None  # 合意で終わった判定を見つけた時刻(段 1 以降に進める印。段 0 の台帳もこのときに書く)
    meet: dict[str, bool] = Field(default_factory=_both_sides_off)
    approve: dict[str, bool] = Field(default_factory=_both_sides_off)
    job_summary: str | None = None  # 段 1 の匿名職務要約(候補者の「会う」のときに書く。生の値なので、台帳には書かない)


@dataclass(frozen=True)
class SideFlags:
    """側ごとのフラグ(「会う」「承認」)。"""

    candidate: bool = False
    employer: bool = False

    def of(self, side: Side) -> bool:
        return self.candidate if side == "candidate" else self.employer

    @property
    def both(self) -> bool:
        return self.candidate and self.employer


@dataclass(frozen=True)
class StageState:
    """stages/{nid} の読み出し(段の遷移に使う項目だけ)。項目が欠けた文書は、段 0・フラグなしと読む。"""

    nid: str
    candidate_principal_id: str | None
    stage: int
    agreed: bool
    meet: SideFlags
    approve: SideFlags
    job_summary: str | None
    employer_template_id: str | None
    candidate_template_id: str | None

    def flags(self, kind: StageKind) -> SideFlags:
        return self.meet if kind == "meet" else self.approve


class CorruptStage(ValueError):
    """段の状態の文書はあるが、段の項目が 0〜2 の整数でない。"""


class StageBusy(Exception):
    """競合で段の状態を書けなかった(再試行を使い切った)。操作は冪等なので、呼び出し側は 503 にして、呼び直してよい。"""


def _flags_from(value: object) -> SideFlags:
    if not isinstance(value, dict):
        return SideFlags()
    return SideFlags(candidate=value.get("candidate") is True, employer=value.get("employer") is True)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _state_from(nid: str, data: dict) -> StageState:
    stage = data.get("stage", 0)
    if isinstance(stage, bool) or not isinstance(stage, int) or not 0 <= stage <= LAST_STAGE:
        raise CorruptStage("stage must be an integer between 0 and 2")
    return StageState(
        nid=nid,
        candidate_principal_id=_optional_str(data.get("candidate_principal_id")),
        stage=stage,
        agreed=data.get("agreed_at") is not None,
        meet=_flags_from(data.get("meet")),
        approve=_flags_from(data.get("approve")),
        job_summary=_optional_str(data.get("job_summary")),
        employer_template_id=_optional_str(data.get("employer_template_id")),
        candidate_template_id=_optional_str(data.get("candidate_template_id")),
    )


@dataclass(frozen=True)
class PressOutcome:
    """press の結果。refused があれば何も書いていない。changed は、この呼び出しでフラグを立てたこと(すでに立っていれば False)。"""

    state: StageState | None  # 押した後の状態。段の状態がなければ None
    refused: PressRefusal | None = None
    changed: bool = False
    advanced_to: int | None = None  # この呼び出しで段が進んだなら、新しい段


def _press_row_id(nid: str, kind: StageKind, side: Side) -> str:
    return f"{nid}-{kind}-{side}"


def _stage_row_id(nid: str, stage: int) -> str:
    return f"{nid}-stage{stage}"


class StageStore:
    """stages/{nid} の作成・遷移・削除・確認。Firestore(同期クライアント)の呼び出しは別スレッドで行う。"""

    def __init__(
        self,
        db: firestore.Client,
        clock: Clock,
        config: RetentionConfig = DEFAULT_WEB_CONFIG.retention,
    ) -> None:
        self._db = db
        self._clock = clock
        self._fictional_ttl = dt.timedelta(seconds=config.fictional_stage_ttl_seconds)
        self._ledger = DisclosureLedger(db)

    def _ref(self, nid: str):
        return self._db.collection(STAGES_COLLECTION).document(nid)

    def _run_transaction(self, txn_fn):
        """txn_fn をトランザクションで行う。競合は、内側の再試行の後に、乱数の待ちを入れて外側でやり直す。使い切れば StageBusy。"""
        last_exc: Exception | None = None
        for attempt in range(_OUTER_ATTEMPTS):
            if attempt > 0:
                time.sleep(random.uniform(*_OUTER_BACKOFF_SECONDS))
            try:
                return firestore.transactional(txn_fn)(self._db.transaction(max_attempts=_INNER_MAX_ATTEMPTS))
            except Aborted as exc:  # トランザクションの中の読み取りで出た競合(内側では再試行されない)
                last_exc = exc
            except ValueError as exc:  # コミットの競合が、内側の再試行を使い切った
                if not isinstance(exc.__cause__, Aborted):
                    raise
                last_exc = exc
        _log.warning("stage update gave up after contention")
        raise StageBusy from last_exc

    def _create_sync(
        self,
        nid: str,
        candidate_principal_id: str | None,
        employer_template_id: str | None,
        candidate_template_id: str | None,
    ) -> bool:
        now = self._clock.now()
        document = StageDocument(
            nid=nid,
            candidate_principal_id=candidate_principal_id,
            stage=0,
            created_at=now,
            ttl_at=now + self._fictional_ttl if candidate_principal_id is None else None,
            employer_template_id=employer_template_id,
            candidate_template_id=candidate_template_id,
        )
        data = document.model_dump(mode="python")
        for optional in ("ttl_at", "employer_template_id", "candidate_template_id", "agreed_at", "job_summary"):
            if data[optional] is None:
                del data[optional]  # 期限・テンプレート ID・要約は、ないときは項目そのものを付けない
        try:
            # create は「なければ作る」を 1 回の書き込みで行う。すでにあれば何も変えない
            # (段が進んだ文書を、段 0 で上書きしない。冪等)。
            self._ref(nid).create(data)
        except AlreadyExists:
            return False
        return True

    async def ensure(
        self,
        nid: str,
        candidate_principal_id: str | None,
        *,
        employer_template_id: str | None = None,
        candidate_template_id: str | None = None,
    ) -> bool:
        """stages/{nid} がなければ段 0 で作る。作ったら True、すでにあれば False(何も変えない)。

        candidate_principal_id が None(候補者が架空人物。デモ・攻撃)なら、96 時間の ttl_at を付ける。
        employer_template_id・candidate_template_id は、デモ・攻撃の自動応答が、フィクスチャを引く元(作成の呼び出し側が分かるときだけ)。
        """
        return await asyncio.to_thread(
            self._create_sync, nid, candidate_principal_id, employer_template_id, candidate_template_id
        )

    def _get_sync(self, nid: str) -> StageState | None:
        if _NID_RE.fullmatch(nid) is None:
            return None  # Firestore の文書 ID にできない形の値は、そもそも交渉 ID ではない
        snap = self._ref(nid).get()
        return _state_from(nid, snap.to_dict()) if snap.exists else None

    async def get(self, nid: str) -> StageState | None:
        """段の状態を読む。なければ None。"""
        return await asyncio.to_thread(self._get_sync, nid)

    def _is_fictional_sync(self, nid: str) -> bool:
        if _NID_RE.fullmatch(nid) is None:
            return False  # Firestore の文書 ID にできない形の値は、そもそも交渉 ID ではない
        snap = self._ref(nid).get()
        if not snap.exists:
            return False
        data = snap.to_dict()
        # 項目が欠けた文書は、架空と読まない(.get(...) is None では、欠けも「架空」になってしまう。台帳 X-38)。
        return "candidate_principal_id" in data and data["candidate_principal_id"] is None

    async def is_fictional_negotiation(self, nid: str) -> bool:
        """nid が、候補者が架空人物の交渉(デモ・攻撃)と分かっているか(§6.3 のデモ用エンドポイントの確認)。

        これは web の補助の確認(金庫の確認が正本。台帳 X-38)。段の状態がない(まだ作っていない)交渉、
        candidate_principal_id の項目が欠けた文書、本物の候補者の交渉、交渉 ID の形でない値は False
        (拒否する側に倒す)。
        """
        return await asyncio.to_thread(self._is_fictional_sync, nid)

    def _record_agreement_sync(self, nid: str) -> Literal["recorded", "already", "absent"]:
        ref = self._ref(nid)

        def txn_fn(txn: firestore.Transaction) -> Literal["recorded", "already", "absent"]:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return "absent"
            data = snap.to_dict()
            if data.get("agreed_at") is not None:
                return "already"
            now = self._clock.now()
            txn.update(ref, dict(agreed_at=now))
            principal_id = _optional_str(data.get("candidate_principal_id"))
            if principal_id is not None:  # 台帳は依頼者ごと。架空の候補者(デモ・攻撃)の交渉には、持ち主がいない
                row = self._disclose_row(principal_id, nid, 0, "system", now)
                txn.set(self._ledger.row_ref(principal_id, _stage_row_id(nid, 0)), row.model_dump(mode="python"))
            return "recorded"

        return self._run_transaction(txn_fn)

    async def record_agreement(self, nid: str) -> Literal["recorded", "already", "absent"]:
        """合意で終わった判定を見つけたことを記録する(冪等)。段 1 以降に進める印で、段 0 の表示を台帳(本物の候補者)に 1 回だけ書く。"""
        return await asyncio.to_thread(self._record_agreement_sync, nid)

    @staticmethod
    def _disclose_row(principal_id: str, nid: str, stage: int, operator: LedgerOperator, now: dt.datetime) -> LedgerRow:
        return LedgerRow(
            principal_id=principal_id,
            nid=nid,
            action="disclose",
            stage=stage,
            operator=operator,
            items=list(OPENED_BY_STAGE[stage]),
            to=RECIPIENT_BY_STAGE[stage],
            simulated=stage == LAST_STAGE,  # 台帳の持ち主は本物の候補者だけ。その段 2 は、連絡先を集めない模擬表示
            at=now,
        )

    def _press_sync(
        self, nid: str, side: Side, kind: StageKind, operator: LedgerOperator, job_summary: str | None
    ) -> PressOutcome:
        ref = self._ref(nid)

        def txn_fn(txn: firestore.Transaction) -> PressOutcome:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return PressOutcome(None, refused="absent")
            state = _state_from(nid, snap.to_dict())
            if not state.agreed:
                return PressOutcome(state, refused="not_agreed")
            if kind == "approve" and state.stage < 1:
                return PressOutcome(state, refused="stage_not_open")  # 承認は、段 1 が開いてから
            flags = state.flags(kind)
            if flags.of(side):
                return PressOutcome(state)  # すでに押してある(冪等。要約も、最初に書いたものを残す)
            now = self._clock.now()
            new_flags = dataclasses.replace(flags, **{side: True})
            updates: dict = {}
            updates[f"{kind}.{side}"] = True
            summary = state.job_summary
            if kind == "meet" and side == "candidate" and job_summary is not None:
                updates["job_summary"] = job_summary
                summary = job_summary
            principal_id = state.candidate_principal_id
            rows: list[tuple[str, LedgerRow]] = []
            if principal_id is not None:
                pressed = LedgerRow(
                    principal_id=principal_id, nid=nid, action=kind, stage=state.stage, operator=operator, at=now
                )
                rows.append((_press_row_id(nid, kind, side), pressed))
            advanced_to = None
            if new_flags.both and state.stage == (0 if kind == "meet" else 1):
                advanced_to = state.stage + 1  # 両方のフラグがそろったトランザクションの中でだけ、次の段へ進む
                updates["stage"] = advanced_to
                if principal_id is not None:
                    opened = self._disclose_row(principal_id, nid, advanced_to, operator, now)
                    rows.append((_stage_row_id(nid, advanced_to), opened))
            txn.update(ref, updates)
            for row_id, row in rows:
                txn.set(self._ledger.row_ref(row.principal_id, row_id), row.model_dump(mode="python"))
            after = dataclasses.replace(
                state, stage=advanced_to or state.stage, job_summary=summary, **{kind: new_flags}
            )
            return PressOutcome(after, changed=True, advanced_to=advanced_to)

        return self._run_transaction(txn_fn)

    async def press(
        self,
        nid: str,
        side: Side,
        kind: StageKind,
        *,
        operator: LedgerOperator,
        job_summary: str | None = None,
    ) -> PressOutcome:
        """side の「会う」(meet)・「承認」(approve)を、フラグとして冪等に立てる。両方がそろったトランザクションの中でだけ、次の段へ進める。

        合意で終わった判定を記録(record_agreement)してある交渉だけ。承認は、段 1 が開いてから。押したこと・段が開いたことは、同じ
        トランザクションで、本物の候補者の台帳に書く(生の値は書かない)。job_summary は、候補者の「会う」でフラグを新しく立てるときだけ
        保存する(2 回目以降は、最初に書いたものを残す)。refused があれば、何も書いていない。
        """
        return await asyncio.to_thread(self._press_sync, nid, side, kind, operator, job_summary)

    def _delete_for_principal_sync(self, principal_id: str) -> int:
        query = self._db.collection(STAGES_COLLECTION).where(
            filter=FieldFilter("candidate_principal_id", "==", principal_id)
        )
        deleted = 0
        while True:
            documents = list(query.limit(_DELETE_BATCH_SIZE).stream())
            if not documents:
                return deleted
            batch = self._db.batch()
            for snap in documents:
                batch.delete(snap.reference)
            batch.commit()
            deleted += len(documents)

    async def delete_for_principal(self, principal_id: str) -> int:
        """principal_id が候補者の段の状態(段 1 の職務要約を含む)をすべて消す(冪等)。消した数を返す。

        金庫の削除が先に済んで、金庫から交渉が消えていても、stages/{nid} に持たせた候補者の依頼者 ID
        で引ける(§6.3 の削除の流れの 3)。
        """
        return await asyncio.to_thread(self._delete_for_principal_sync, principal_id)


class SideFlagsView(BaseModel):
    """側ごとのフラグ(「会う」「承認」を押したか)。"""

    model_config = ConfigDict(extra="forbid")

    candidate: bool
    employer: bool


class EmployerDisclosure(BaseModel):
    """求人側に見えているもの(§6.2 の表。EMPLOYER_SEES)。visible が、いま見えている項目の種類。見えていない項目は null。

    simulated は、段 2 の氏名・連絡先が模擬表示であること(実ユーザーは連絡先を集めない。P-2)。このとき visible には name・email があるが、
    値は null(「ここで連絡先が開示されます」と見せるだけ)。
    """

    model_config = ConfigDict(extra="forbid")

    visible: list[Item]
    likelihood: Likelihood | None = None
    package: Package | None = None
    job_summary: str | None = None
    name: str | None = None
    email: str | None = None
    simulated: bool = False


class CompanyView(BaseModel):
    """候補者に見える、求人の企業名。非公開求人(confidential)は、段 1 まで null(FR-32)。"""

    model_config = ConfigDict(extra="forbid")

    confidential: bool
    name: str | None


class StageView(BaseModel):
    """候補者(本人。デモ・攻撃では架空の候補者)から見た、段階開示の状態。version・相手の残り回数・理由は含めない(DV-10)。

    judged が False の間は、見込みを出さない(FR-26: 見込みは終了時に 1 回だけ。result は null)。agreed が False で judged は、見込み「なし」
    (段 0 の表示だけで終わる)。employer_fictional・employer_auto_response は、画面が「架空の求人(自動応答)」と明示するための印。
    """

    model_config = ConfigDict(extra="forbid")

    nid: str
    judged: bool
    agreed: bool
    stage: int
    result: NegotiationResult | None
    meet: SideFlagsView
    approve: SideFlagsView
    employer_fictional: bool
    employer_auto_response: bool
    company: CompanyView | None
    disclosed_to_employer: EmployerDisclosure


class StageRefused(Exception):
    """段階開示の操作を、いまは受け付けない(呼び出し側は 409 にする)。reason は列挙値。"""

    def __init__(self, reason: Literal["not_judged", "not_agreed", "stage_not_open"]) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class NegotiationFacts:
    """段階開示が、金庫から読んだ交渉の事実。judged は終わったか、result は最終記録(§3.2 の final_result)。"""

    nid: str
    candidate_principal_id: str | None  # 候補者が架空人物(デモ・攻撃)なら None
    job_id: str | None  # 本物の候補者の交渉の求人 ID(金庫の交渉の文書から。求人のフィクスチャを引く)
    judged: bool
    result: NegotiationResult | None

    @property
    def agreed(self) -> bool:
        """合意で終わったか。段 1 以降に進めるのは、これだけ(見込み「なし」は、段 0 の表示で終わり)。"""
        return (
            self.judged
            and self.result is not None
            and self.result.likelihood != "none"
            and self.result.package is not None
        )


@dataclass(frozen=True)
class Settled:
    """自動応答まで済ませた、いまの段の状態と、その交渉のフィクスチャ(分からなければ None)。"""

    state: StageState
    employer: EmployerFixture | None
    candidate: CandidateFixture | None


class StageFlow:
    """段階開示の流れ。段の状態(StageStore)・金庫・フィクスチャを組み合わせて、段の表示・「会う」「承認」・架空人物の自動応答を行う。

    FixtureLookup(web.fictional_answerer)も満たす: 交渉 ID から、その交渉のフィクスチャを引く(途中確認の自動回答の元)。
    依頼者ごとのロック(台帳 I-4)は、ここでは取らない。本人の操作は、リクエストを受けたミドルウェアがすでに持っている。
    """

    def __init__(
        self,
        *,
        stages: StageStore,
        vault: VaultClient,
        fixtures: FixtureCatalog,
        config: StagesConfig = DEFAULT_STAGES_CONFIG,
    ) -> None:
        self._stages = stages
        self._vault = vault
        self._fixtures = fixtures
        self._config = config

    @property
    def config(self) -> StagesConfig:
        return self._config

    # ------------------------------------------------------------------
    # 交渉の事実
    # ------------------------------------------------------------------

    @staticmethod
    def facts_for_principal(summary: PrincipalNegotiationSummary, principal_id: str) -> NegotiationFacts:
        """本人が当事者の交渉(金庫の一覧の 1 件)から。終わった交渉(state=ended)の最終結果が、段 0 の中身。"""
        judged = summary.state == "ended"
        return NegotiationFacts(
            nid=summary.nid,
            candidate_principal_id=principal_id,
            job_id=summary.job_id,
            judged=judged,
            result=summary.result if judged else None,
        )

    @staticmethod
    def facts_for_fictional(nid: str, events: list[EventViewItem]) -> NegotiationFacts:
        """候補者が架空人物の交渉(デモ・攻撃)から。金庫のデモ用の読み出しで読んだ、候補者側の見え方の最終記録が、段 0 の中身。"""
        final = next((event for event in reversed(events) if event.kind == "final_result"), None)
        return NegotiationFacts(
            nid=nid,
            candidate_principal_id=None,
            job_id=None,
            judged=final is not None,
            result=final.result if final is not None else None,
        )

    # ------------------------------------------------------------------
    # フィクスチャを引く
    # ------------------------------------------------------------------

    def _employer_from(self, state: StageState, job_id: str | None) -> EmployerFixture | None:
        if state.employer_template_id is not None:  # デモ・攻撃: 作成のときに控えたテンプレート ID
            return self._fixtures.employer_by_template(state.employer_template_id)
        if job_id is not None:  # 本物の候補者: 金庫の交渉の文書の job_id(§6.2)
            return self._fixtures.employer_by_job(job_id)
        return None

    def _candidate_from(self, state: StageState) -> CandidateFixture | None:
        if state.candidate_template_id is None:
            return None
        return self._fixtures.candidate_by_template(state.candidate_template_id)

    async def employer_fixture(self, nid: str) -> EmployerFixture | None:
        """nid の求人のフィクスチャ(分からなければ None)。本物の候補者の交渉は、金庫の交渉の job_id から引く。"""
        state = await self._stages.get(nid)
        if state is None:
            return None
        job_id = None
        if state.employer_template_id is None and state.candidate_principal_id is not None:
            summaries = await self._vault.list_principal_negotiations(state.candidate_principal_id)
            job_id = next((summary.job_id for summary in summaries if summary.nid == nid), None)
        return self._employer_from(state, job_id)

    async def candidate_fixture(self, nid: str) -> CandidateFixture | None:
        """nid の架空の候補者のフィクスチャ(本物の候補者の交渉・分からない交渉は None)。"""
        state = await self._stages.get(nid)
        return self._candidate_from(state) if state is not None else None

    async def candidate_bands(self, nid: str) -> CandidateAttributeBands | None:
        """金庫が持つ、候補者の属性帯(求人側の view の counterparty)。求人の規則を選ぶ元。なければ None。"""
        try:
            view = await self._vault.get_view(nid, "employer")
        except VaultNotFoundError:
            return None
        return view.counterparty if isinstance(view.counterparty, CandidateAttributeBands) else None

    # ------------------------------------------------------------------
    # 段の状態を整える(段の状態の作成・判定の記録・架空人物の自動応答)
    # ------------------------------------------------------------------

    async def _read(self, nid: str) -> StageState:
        state = await self._stages.get(nid)
        if state is None:
            raise StageBusy  # 作った直後に消えた(削除と競合した)。操作は冪等なので、呼び直してよい
        return state

    async def settle(self, facts: NegotiationFacts) -> Settled:
        """段の状態を整える。なければ作り(画面で交渉を開いたとき。台帳 L9-4)、合意なら判定を記録して、架空人物の自動応答を行う。すべて冪等。"""
        state = await self._stages.get(facts.nid)
        if state is None:
            await self._stages.ensure(facts.nid, facts.candidate_principal_id)  # 判定の後に作り損ねた段の状態を、開いたときに作る(台帳 L9-4)
            state = await self._read(facts.nid)
        if state.candidate_principal_id != facts.candidate_principal_id:
            raise CorruptStage("the stage document belongs to another kind of candidate")
        employer = self._employer_from(state, facts.job_id)
        candidate = self._candidate_from(state)
        if facts.agreed:
            if not state.agreed:
                await self._stages.record_agreement(facts.nid)
                state = await self._read(facts.nid)
            state = await self._auto_respond(facts, state, employer, candidate)
        return Settled(state, employer, candidate)

    async def _auto_respond(
        self,
        facts: NegotiationFacts,
        state: StageState,
        employer: EmployerFixture | None,
        candidate: CandidateFixture | None,
    ) -> StageState:
        """架空人物の「会う」「承認」を、サーバが自動で押す(P-2)。押せるものを順に押す(冪等。押し済み・まだ押せないものは飛ばす)。

        求人側は、フィクスチャの設定(auto_response)に従う。デモ・攻撃の架空の候補者は、フィクスチャの職務要約・連絡先で押す。
        本物の候補者は、本人が押す(ここでは押さない)。順番は、双方の「会う」→ 双方の「承認」(承認は段 1 が開いてから)。
        """
        steps: list[tuple[Side, StageKind, LedgerOperator, str | None]] = []
        fictional_candidate = facts.candidate_principal_id is None and candidate is not None
        if employer is not None and employer.auto_response.meet:
            steps.append(("employer", "meet", "fictional_employer", None))
        if fictional_candidate:
            steps.append(("candidate", "meet", "fictional_candidate", candidate.job_summary))
        if employer is not None and employer.auto_response.approve:
            steps.append(("employer", "approve", "fictional_employer", None))
        if fictional_candidate:
            steps.append(("candidate", "approve", "fictional_candidate", None))
        for side, kind, operator, job_summary in steps:
            if state.flags(kind).of(side) or (kind == "approve" and state.stage < 1):
                continue
            outcome = await self._stages.press(facts.nid, side, kind, operator=operator, job_summary=job_summary)
            if outcome.state is not None:
                state = outcome.state
        return state

    # ------------------------------------------------------------------
    # 表示と操作(候補者側。求人側の操作はサーバの自動応答だけ)
    # ------------------------------------------------------------------

    async def view(self, facts: NegotiationFacts) -> StageView:
        """候補者から見た段の状態。見るたびに、段の状態を整える(settle)。"""
        return self._build_view(facts, await self.settle(facts))

    @staticmethod
    def _require_agreed(facts: NegotiationFacts) -> None:
        if not facts.judged:
            raise StageRefused("not_judged")
        if not facts.agreed:
            raise StageRefused("not_agreed")

    async def _press_as_candidate(
        self, facts: NegotiationFacts, kind: StageKind, settled: Settled, job_summary: str | None
    ) -> StageView:
        outcome = await self._stages.press(facts.nid, "candidate", kind, operator="principal", job_summary=job_summary)
        if outcome.state is None:
            raise StageBusy  # 押す直前に消えた(削除と競合した)。冪等なので、呼び直してよい
        if outcome.refused == "stage_not_open":
            raise StageRefused("stage_not_open")
        if outcome.refused is not None:
            raise StageRefused("not_agreed")
        state = await self._auto_respond(facts, outcome.state, settled.employer, settled.candidate)
        return self._build_view(facts, Settled(state, settled.employer, settled.candidate))

    async def meet(self, facts: NegotiationFacts, job_summary: str) -> StageView:
        """候補者の「会う」(匿名職務要約つき)。合意で終わった交渉だけ。すでに押してあれば何も変えない(要約は最初のものを残す)。"""
        self._require_agreed(facts)
        settled = await self.settle(facts)
        return await self._press_as_candidate(facts, "meet", settled, job_summary)

    async def approve(self, facts: NegotiationFacts) -> StageView:
        """候補者の「承認」。段 1 が開いてから(双方が「会う」を押した後)。すでに押してあれば何も変えない。"""
        self._require_agreed(facts)
        settled = await self.settle(facts)
        if settled.state.stage < 1:
            raise StageRefused("stage_not_open")
        return await self._press_as_candidate(facts, "approve", settled, None)

    @staticmethod
    def _build_view(facts: NegotiationFacts, settled: Settled) -> StageView:
        state, employer, candidate = settled.state, settled.employer, settled.candidate
        agreed = facts.agreed
        stage = state.stage if agreed else 0
        result = facts.result if facts.judged else None
        if not facts.judged:
            visible: Sequence[Item] = ()
        elif not agreed:
            visible = EMPLOYER_SEES_WITHOUT_AGREEMENT
        else:
            visible = EMPLOYER_SEES[stage]
        # 実ユーザー(本物の候補者)の段 2 は模擬表示: 連絡先を集めていないので、値は出さない(P-2)。
        simulated = agreed and stage == LAST_STAGE and state.candidate_principal_id is not None
        disclosed = EmployerDisclosure(visible=list(visible), simulated=simulated)
        if result is not None and "likelihood" in visible:
            disclosed.likelihood = result.likelihood
        if result is not None and "package" in visible:
            disclosed.package = result.package
        if "job_summary" in visible:
            disclosed.job_summary = state.job_summary
        if candidate is not None and not simulated and "name" in visible:
            disclosed.name = candidate.contact.name
        if candidate is not None and not simulated and "email" in visible:
            disclosed.email = candidate.contact.email
        company = None
        if employer is not None:
            confidential = employer.public_job.confidential
            shown = not confidential or (agreed and stage >= COMPANY_NAME_STAGE)
            company = CompanyView(confidential=confidential, name=employer.company_name if shown else None)
        return StageView(
            nid=facts.nid,
            judged=facts.judged,
            agreed=agreed,
            stage=stage,
            result=result,
            meet=SideFlagsView(candidate=state.meet.candidate, employer=state.meet.employer),
            approve=SideFlagsView(candidate=state.approve.candidate, employer=state.approve.employer),
            employer_fictional=True,  # 求人側は、ハッカソンではいつもフィクスチャ(§3.7)
            employer_auto_response=employer is not None and (employer.auto_response.meet or employer.auto_response.approve),
            company=company,
            disclosed_to_employer=disclosed,
        )
