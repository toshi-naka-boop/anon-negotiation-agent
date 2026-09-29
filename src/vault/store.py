"""金庫の状態機械(design.md §3.1・§3.2・§3.4・§3.5・§3.6)。

Firestore のトランザクションで、状態遷移・回数の消費・イベント列への記録を必ず一度に行う。
秘密に触れる評価(negotiation_core.evaluate)は、このモジュールとその配下だけが呼ぶ。
"""

import datetime as dt
import hashlib
from dataclasses import dataclass

from google.api_core.exceptions import Aborted
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from negotiation_core import (
    AXIS_KEYS,
    Budget,
    CandidateAttributeBands,
    EvaluatedPackage,
    Policy,
    Side,
    Verdict,
    evaluate,
)

from vault import budget as budget_math
from vault import principal_answer
from vault.api_models import (
    ControlRequest,
    ControlResponse,
    CreateNegotiationRequest,
    CreateNegotiationResponse,
    EventViewItem,
    ExpireResponse,
    MoveRequest,
    MoveResponse,
    NegotiationViewResponse,
    OpenNegotiationsPage,
    OpenNegotiationSummary,
    PolicyView,
    PrincipalAnswerRequest,
    PrincipalAnswerResponse,
    PrincipalNegotiationSummary,
    PutBlocklistRequest,
    PutPolicyRequest,
)
from vault.clock import Clock
from vault.config import VaultConfig
from vault.errors import (
    MovePreconditionFailed,
    NotFoundError,
    PolicyValidationError,
    PrincipalDeletingError,
    TransactionRetryExhausted,
)
from vault.ids import generate_id
from vault.judgment import judge
from vault.models import (
    CandidateTemplate,
    CountersBySide,
    EmployerTemplate,
    EndReason,
    EvaluationBudgetWindow,
    EventRecord,
    EventView,
    EventViews,
    NegotiationDocument,
    NegotiationResult,
    Participant,
    Participants,
    PendingOffer,
    PendingQuestion,
    SideCounters,
    Snapshots,
)
from vault.serialization import model_from_firestore, model_to_firestore
from vault.stop_rule import determine_stop_reason
from vault.templates import TEMPLATES_COLLECTION, resolve_employer_policy

NEGOTIATIONS_COLLECTION = "negotiations"
PRINCIPALS_COLLECTION = "principals"
IDEMPOTENCY_COLLECTION = "idempotency"
EVENTS_SUBCOLLECTION = "events"


@dataclass
class _MoveOutcome:
    """1 つの手を処理した結果(内部専用。API には出さない)。"""

    valid: bool
    error: str | None
    mover_view: EventView
    counterparty_view: EventView | None
    # 有効な accept・end のときだけ (end_reason, result) を持つ(§3.2: この 2 つは
    # 「終了」の行そのものであり、別行の記録を持たない)。
    terminate: tuple[EndReason, NegotiationResult] | None


class VaultStore:
    """金庫の状態機械の実装。Firestore クライアント・時計・暫定値を受け取る。"""

    def __init__(self, db: firestore.Client, clock: Clock, config: VaultConfig) -> None:
        self._db = db
        self._clock = clock
        self._config = config

    # ------------------------------------------------------------------
    # 文書の参照
    # ------------------------------------------------------------------

    def _negotiations(self):
        return self._db.collection(NEGOTIATIONS_COLLECTION)

    def _negotiation_ref(self, nid: str):
        return self._negotiations().document(nid)

    def _events(self, nid: str):
        return self._negotiation_ref(nid).collection(EVENTS_SUBCOLLECTION)

    def _principal_ref(self, pid: str):
        return self._db.collection(PRINCIPALS_COLLECTION).document(pid)

    def _idempotency_ref(self, request_id: str):
        key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self._db.collection(IDEMPOTENCY_COLLECTION).document(key)

    def _new_transaction(self) -> firestore.Transaction:
        # 既定の max_attempts=5 は、DV-02 が求める高い並行度(同一文書への 10 並行アクセス)
        # のもとでは、正しい手続き(1 本だけ通り、残りは何も消費しない)が完了する前に
        # 「再試行を使い切った」という別種のエラーで終わってしまうことがある
        # (Firestore エミュレータの粗いロック実装で顕著)。中身(何が起きたら勝つか)は
        # 変えず、再試行の回数だけ増やす。それでも尽きた場合の扱いは _run_transaction。
        return self._db.transaction(max_attempts=20)

    def _run_transaction(self, txn_fn, *, contention_error: type[Exception]):
        """@firestore.transactional で txn_fn(txn) を実行する(呼び出し口を 1 つに集約)。

        トランザクションの再試行を使い切ると、google-cloud-firestore は Aborted を
        包んだ ValueError を投げる。ここでだけ、操作の種類に応じた例外に変換する:
        手の操作(moves)は MovePreconditionFailed(409。何も消費していない点は
        expected_version の不一致と同じ)、冪等な操作(control・expire)と作成は
        TransactionRetryExhausted(503。再試行してよい。DV-02「409 にならない」)。
        """
        transaction = self._new_transaction()
        wrapped = firestore.transactional(txn_fn)
        try:
            return wrapped(transaction)
        except ValueError as exc:
            if isinstance(exc.__cause__, Aborted):
                raise contention_error("transaction retries exhausted under contention") from exc
            raise

    # ------------------------------------------------------------------
    # §3.8 手順 1 のガード(差し戻し対応 3): 依頼者が deleting なら、それが関わる操作を拒否する。
    # 「依頼者を単位にする操作」(PUT/GET policy・PUT blocklist・本人の交渉一覧・作成)と、
    # 「交渉を単位にする操作」(moves・control・view・events)の両方から使う共通下請け。
    # ------------------------------------------------------------------

    def _is_participant_deleting(
        self, participant: Participant, txn: firestore.Transaction | None = None
    ) -> bool:
        """participant が本物(is_fictional=False)で、principals/{pid} が deleting なら True。

        架空人物(テンプレート)には deleting の概念がないので、常に False。
        txn を渡せば、そのトランザクションの一部として読む(moves・control で使う)。
        """
        if participant.is_fictional or participant.principal_id is None:
            return False
        snap = self._principal_ref(participant.principal_id).get(transaction=txn)
        return snap.exists and snap.to_dict().get("deleting", False)

    def _reject_if_any_participant_deleting(
        self, doc: NegotiationDocument, nid: str, txn: firestore.Transaction | None = None
    ) -> None:
        """交渉を単位にする操作(moves・control)向け: 本物の参加者のどちらかが deleting なら
        拒否する(§3.8 手順 1「以後、その依頼者が関わる操作はすべて拒否する」)。手番・side を
        問わない: 交渉そのものが、間もなく手順 2 で取消になる対象だから。
        """
        if self._is_participant_deleting(doc.participants.candidate, txn) or self._is_participant_deleting(
            doc.participants.employer, txn
        ):
            raise PrincipalDeletingError(f"a real participant of negotiation {nid!r} is being deleted")

    def _reject_if_side_participant_deleting(self, doc: NegotiationDocument, side: Side, nid: str) -> None:
        """読み出し(view・events)向け: 指定された側の本物の参加者が deleting なら拒否する。
        相手側の読み出しは許す(相手が本物のとき、相手の記録は相手のものとして残るため)。
        """
        participant = doc.participants.candidate if side == "candidate" else doc.participants.employer
        if self._is_participant_deleting(participant):
            raise PrincipalDeletingError(f"principal for side={side!r} of negotiation {nid!r} is being deleted")

    def _reject_if_principal_deleting(self, pid: str, snap=None) -> None:
        """依頼者を単位にする操作向け(PUT/GET policy・PUT blocklist・本人の交渉一覧): pid が
        deleting なら拒否する。すでに読み込み済みの DocumentSnapshot があれば渡せる
        (get_policy のように、この後で同じ snap を使う場合に二重に読まないため)。
        """
        if snap is None:
            snap = self._principal_ref(pid).get()
        if snap.exists and snap.to_dict().get("deleting", False):
            raise PrincipalDeletingError(f"principal {pid!r} is being deleted")

    # ------------------------------------------------------------------
    # §3.3 PUT/GET .../policy、PUT .../blocklist
    # ------------------------------------------------------------------

    def put_policy(self, pid: str, request: PutPolicyRequest) -> None:
        """丸め済みポリシーを置き換える。グリッド外・矛盾の拒否は Policy 自身が行う
        (pydantic の検証。FastAPI の場合はリクエスト解析の時点で 422 になる)。

        attribute_bands(候補者側)が指定されていれば一緒に保存する(台帳 I-2)。
        指定がなければ、すでに保存済みの値はそのまま残す(この呼び出しでは消さない)。
        """
        unknown_axes = [a for a in request.removed_axes if a not in AXIS_KEYS]
        if unknown_axes:
            raise PolicyValidationError(f"unknown axes in removed_axes: {unknown_axes}")
        self._reject_if_principal_deleting(pid)
        update = {
            "policy": model_to_firestore(request.policy),
            "removed_axes": list(request.removed_axes),
        }
        if request.attribute_bands is not None:
            update["attribute_bands"] = model_to_firestore(request.attribute_bands)
        self._principal_ref(pid).set(update, merge=True)

    def get_policy(self, pid: str) -> PolicyView:
        snap = self._principal_ref(pid).get()
        self._reject_if_principal_deleting(pid, snap)
        data = snap.to_dict() if snap.exists else None
        if not data or data.get("policy") is None:
            raise NotFoundError(f"principal has no policy: {pid}")
        bands_data = data.get("attribute_bands")
        return PolicyView(
            policy=model_from_firestore(Policy, data["policy"]),
            removed_axes=list(data.get("removed_axes", [])),
            attribute_bands=model_from_firestore(CandidateAttributeBands, bands_data)
            if bands_data
            else None,
        )

    def put_blocklist(self, pid: str, request: PutBlocklistRequest) -> None:
        """候補者のブロック先(企業 ID)を置き換える(§3.3)。"""
        self._reject_if_principal_deleting(pid)
        self._principal_ref(pid).set({"blocklist": list(request.blocklist)}, merge=True)

    # ------------------------------------------------------------------
    # §3.5 交渉の作成
    # ------------------------------------------------------------------

    def create_negotiation(self, request: CreateNegotiationRequest) -> CreateNegotiationResponse:
        idem_ref = self._idempotency_ref(request.request_id)
        neg_col = self._negotiations()

        def _txn(txn: firestore.Transaction) -> CreateNegotiationResponse:
            now = self._clock.now()

            # --- 冪等性: 同じ request_id にはすでに作った交渉を返す ---
            idem_snap = idem_ref.get(transaction=txn)
            if idem_snap.exists:
                nid = idem_snap.to_dict()["nid"]
                neg_snap = neg_col.document(nid).get(transaction=txn)
                existing = model_from_firestore(NegotiationDocument, neg_snap.to_dict())
                return CreateNegotiationResponse(status="created", nid=nid, version=existing.version)

            # --- 候補者側を解決する(読み取りだけ。書き込みより前に行う) ---
            cand_req = request.candidate
            candidate_principal_snap = None
            if cand_req.is_fictional:
                template_snap = (
                    self._db.collection(TEMPLATES_COLLECTION).document(cand_req.template_id).get(transaction=txn)
                )
                if not template_snap.exists:
                    raise NotFoundError(f"candidate template not found: {cand_req.template_id}")
                candidate_template = model_from_firestore(CandidateTemplate, template_snap.to_dict())
                candidate_policy = candidate_template.policy
                candidate_attribute_bands = candidate_template.attribute_bands
                candidate_participant = Participant(
                    is_fictional=True,
                    template_id=cand_req.template_id,
                    attribute_bands=candidate_attribute_bands,
                )
            else:
                candidate_principal_snap = self._principal_ref(cand_req.principal_id).get(transaction=txn)
                principal_data = candidate_principal_snap.to_dict() if candidate_principal_snap.exists else None
                if not principal_data or principal_data.get("policy") is None:
                    raise NotFoundError(f"candidate principal has no policy: {cand_req.principal_id}")
                # 差し戻し対応 3(§3.8 手順 1): 削除中の依頼者は、新しい交渉を作れない。
                # ここは作成のトランザクションの中で依頼者の文書を読む箇所なので、この読み取りと
                # delete_principal の手順 1(deleting を立てる書き込み)との競合は、
                # Firestore のトランザクションの再試行(このトランザクションが読んだ文書が
                # コミット前に変わっていれば abort・再試行される)によって解決される。
                if principal_data.get("deleting", False):
                    return CreateNegotiationResponse(status="refused", reason="principal_deleting")
                candidate_policy = model_from_firestore(Policy, principal_data["policy"])
                # 台帳 I-2: 本物の候補者の属性帯は、作成のたびに web から渡させず、
                # principals/{pid} に保存済みの値を金庫が読む(交渉ごとに違う帯を渡せると、
                # 求人側の帯ごとのルールを探れてしまうため)。まだ保存されていなければ、
                # 交渉もイベントも回数も作らずに断る。
                bands_data = principal_data.get("attribute_bands")
                if not bands_data:
                    return CreateNegotiationResponse(status="refused", reason="attribute_bands_missing")
                candidate_attribute_bands = model_from_firestore(CandidateAttributeBands, bands_data)
                candidate_participant = Participant(
                    is_fictional=False,
                    principal_id=cand_req.principal_id,
                    attribute_bands=candidate_attribute_bands,
                )

            # --- 求人側を解決する(§3.7 最終行により常にテンプレート) ---
            employer_template_snap = (
                self._db.collection(TEMPLATES_COLLECTION).document(request.employer.template_id).get(transaction=txn)
            )
            if not employer_template_snap.exists:
                raise NotFoundError(f"employer template not found: {request.employer.template_id}")
            employer_template = model_from_firestore(EmployerTemplate, employer_template_snap.to_dict())
            employer_policy = resolve_employer_policy(employer_template, candidate_attribute_bands)
            employer_participant = Participant(
                is_fictional=True,
                template_id=request.employer.template_id,
                job_id=employer_template.job_id,
                company_id=employer_template.company_id,
            )

            # --- ブロック先(AC-16): 断るなら、交渉もイベントも回数も作らずここで抜ける ---
            if not cand_req.is_fictional:
                blocklist = candidate_principal_snap.to_dict().get("blocklist", [])
                if employer_template.company_id in blocklist:
                    return CreateNegotiationResponse(status="refused", reason="blocked")

            # --- 本物の候補者は進行中(judged でない)の交渉を同時に 1 件までしか持てない ---
            if not cand_req.is_fictional:
                existing_query = neg_col.where(
                    filter=FieldFilter("participants.candidate.principal_id", "==", cand_req.principal_id)
                )
                for snap in existing_query.get(transaction=txn):
                    if snap.to_dict().get("status") != "judged":
                        return CreateNegotiationResponse(status="refused", reason="already_active")

            # --- 予算の予約(本物の依頼者だけ) ---
            new_budget_window = None
            if not cand_req.is_fictional:
                window_data = candidate_principal_snap.to_dict().get("evaluation_budget")
                existing_window = model_from_firestore(EvaluationBudgetWindow, window_data) if window_data else None
                ok, new_budget_window = budget_math.reserve(
                    existing_window,
                    now,
                    self._config.limits.daily_evaluation_budget_per_principal,
                    self._config.limits.evaluation_budget_per_side,
                )
                if not ok:
                    return CreateNegotiationResponse(status="refused", reason="budget_exhausted")

            # --- ここまで来たら作成する ---
            nid = generate_id()
            lifetime = dt.timedelta(seconds=self._config.deadlines.negotiation_lifetime_seconds)
            move_deadline = dt.timedelta(seconds=self._config.deadlines.move_deadline_seconds)
            expires_at = now + lifetime
            deadline = min(now + move_deadline, expires_at)
            ttl_at = None
            if request.mode != "live":
                ttl_at = now + dt.timedelta(seconds=self._config.fictional_negotiation_ttl_seconds)

            doc = NegotiationDocument(
                nid=nid,
                status="active",
                paused=False,
                version=0,
                to_move="candidate",
                counters=CountersBySide(),
                created_at=now,
                expires_at=expires_at,
                deadline=deadline,
                snapshots=Snapshots(candidate=candidate_policy, employer=employer_policy),
                participants=Participants(candidate=candidate_participant, employer=employer_participant),
                request_id=request.request_id,
                mode=request.mode,
                ttl_at=ttl_at,
            )

            txn.set(idem_ref, {"nid": nid, "created_at": now})
            txn.set(neg_col.document(nid), model_to_firestore(doc))
            if not cand_req.is_fictional and new_budget_window is not None:
                txn.set(
                    self._principal_ref(cand_req.principal_id),
                    {"evaluation_budget": model_to_firestore(new_budget_window)},
                    merge=True,
                )

            return CreateNegotiationResponse(status="created", nid=nid, version=0)

        return self._run_transaction(_txn, contention_error=TransactionRetryExhausted)

    # ------------------------------------------------------------------
    # §3.4 期限切れの判定・終了処理の下請け
    # ------------------------------------------------------------------

    def _is_expired(self, doc: NegotiationDocument, now: dt.datetime) -> bool:
        if now >= doc.expires_at:
            return True
        if doc.deadline is not None and now >= doc.deadline:
            return True
        if doc.paused and doc.paused_at is not None:
            max_pause = dt.timedelta(seconds=self._config.deadlines.max_pause_seconds)
            if now - doc.paused_at >= max_pause:
                return True
        return False

    def _terminate(self, doc: NegotiationDocument, reason: EndReason, result: NegotiationResult) -> None:
        """§3.1 終了処理。result を書き、snapshots を消す(FR-14)。イベントの記録・
        Firestore への書き込みは呼び出し側が行う。
        """
        doc.status = "judged"
        doc.end_reason = reason
        doc.result = result
        doc.snapshots = None
        doc.deadline = None

    def _assign_seq(self, doc: NegotiationDocument, side: Side, view: EventView) -> EventView:
        if side == "candidate":
            doc.seq.candidate += 1
            view.seq = doc.seq.candidate
        else:
            doc.seq.employer += 1
            view.seq = doc.seq.employer
        return view

    def _record_event(
        self,
        txn: firestore.Transaction,
        nid: str,
        doc: NegotiationDocument,
        candidate_view: EventView | None,
        employer_view: EventView | None,
    ) -> None:
        """イベント列に 1 レコード書く。version を 1 進め、その値を文書 ID にも使う(§3.2)。"""
        doc.version += 1
        record = EventRecord(
            version=doc.version,
            views=EventViews(candidate=candidate_view, employer=employer_view),
            ttl_at=doc.ttl_at,
        )
        doc_id = f"{doc.version:08d}"
        txn.set(self._events(nid).document(doc_id), model_to_firestore(record))

    def _write_shared_termination(
        self, txn: firestore.Transaction, nid: str, doc: NegotiationDocument, reason: EndReason
    ) -> None:
        """双方に同じ最終結果を見せる終了処理(§3.1・§3.2)。"""
        result = doc.result if doc.result is not None else NegotiationResult(likelihood="none", package=None)
        self._terminate(doc, reason, result)
        candidate_view = EventView(seq=0, kind="final_result", result=result)
        employer_view = EventView(seq=0, kind="final_result", result=result)
        self._assign_seq(doc, "candidate", candidate_view)
        self._assign_seq(doc, "employer", employer_view)
        self._record_event(txn, nid, doc, candidate_view, employer_view)

    # ------------------------------------------------------------------
    # §3.5 手の処理
    # ------------------------------------------------------------------

    def process_move(self, nid: str, request: MoveRequest) -> MoveResponse:
        negotiation_ref = self._negotiation_ref(nid)

        def _txn(txn: firestore.Transaction) -> MoveResponse:
            snap = negotiation_ref.get(transaction=txn)
            if not snap.exists:
                raise NotFoundError(nid)
            doc = model_from_firestore(NegotiationDocument, snap.to_dict())
            now = self._clock.now()

            # §3.4: 期限切れの判定は、トランザクションの最初(expected_version 等より前)で行う。
            if doc.status != "judged" and self._is_expired(doc, now):
                self._write_shared_termination(txn, nid, doc, "timeout")
                txn.set(negotiation_ref, model_to_firestore(doc))
                return MoveResponse(version=doc.version, status=doc.status, valid=True, end_reason="timeout")

            # 差し戻し対応 3(§3.8 手順 1): 本物の参加者(どちらか)が削除中なら拒否する。
            self._reject_if_any_participant_deleting(doc, nid, txn)

            # --- 前提(§3.5)。どれか崩れていれば 409、何も消費しない ---
            if doc.status != "active":
                raise MovePreconditionFailed(f"status is {doc.status!r}, not active")
            if doc.paused:
                raise MovePreconditionFailed("negotiation is paused")
            if doc.version != request.expected_version:
                raise MovePreconditionFailed(
                    f"expected_version mismatch: have {doc.version}, got {request.expected_version}"
                )
            if doc.to_move != request.side:
                raise MovePreconditionFailed(f"turn mismatch: to_move={doc.to_move}, side={request.side}")

            side = request.side
            other_side: Side = "employer" if side == "candidate" else "candidate"
            counters = doc.counters.candidate if side == "candidate" else doc.counters.employer

            # --- 停止の判定(手を処理する前。ポリシーを見ない。§3.5・AC-06) ---
            pre_stop = determine_stop_reason(
                moves_used=counters.moves_used,
                consecutive_invalid=counters.consecutive_invalid,
                moves_budget=self._config.limits.moves_budget_per_side,
                consecutive_invalid_limit=self._config.limits.consecutive_invalid_limit,
            )
            if pre_stop is not None:
                self._write_shared_termination(txn, nid, doc, pre_stop)
                txn.set(negotiation_ref, model_to_firestore(doc))
                return MoveResponse(version=doc.version, status=doc.status, valid=True, end_reason=pre_stop)

            outcome = self._apply_move_by_kind(doc, side, other_side, counters, request, now)

            # --- 連続無効手のリセット/加算(手の種類を問わない) ---
            if outcome.valid:
                counters.consecutive_invalid = 0
            else:
                counters.consecutive_invalid += 1

            # --- 手数を進める(有効な check だけ除く。無効な check を含め、それ以外の
            #     手はすべて数える。§3.5「手数(check を除く。無効手を含む)」の
            #     「check を除く」は有効な check のことと読む: 差し戻しの指摘どおり) ---
            if not (request.move == "check" and outcome.valid):
                counters.moves_used += 1

            if outcome.terminate is not None:
                end_reason, result = outcome.terminate
                self._terminate(doc, end_reason, result)
                candidate_view = outcome.mover_view if side == "candidate" else outcome.counterparty_view
                employer_view = outcome.counterparty_view if side == "candidate" else outcome.mover_view
                self._assign_seq(doc, "candidate", candidate_view)
                self._assign_seq(doc, "employer", employer_view)
                self._record_event(txn, nid, doc, candidate_view, employer_view)
                txn.set(negotiation_ref, model_to_firestore(doc))
                return MoveResponse(version=doc.version, status=doc.status, valid=True, end_reason=end_reason)

            # --- 通常の(終了ではない)記録 ---
            self._assign_seq(doc, side, outcome.mover_view)
            if outcome.counterparty_view is not None:
                self._assign_seq(doc, other_side, outcome.counterparty_view)
            candidate_view = outcome.mover_view if side == "candidate" else outcome.counterparty_view
            employer_view = outcome.counterparty_view if side == "candidate" else outcome.mover_view
            self._record_event(txn, nid, doc, candidate_view, employer_view)

            # --- 無効手により、手数または連続無効手が上限に達したら、同じトランザクションで
            #     もう 1 レコード(終了)を書く(§3.1: 「3 回目の連続無効手...2 件」と同じ形)。
            end_reason_from_chain: EndReason | None = None
            if not outcome.valid:
                post_stop = determine_stop_reason(
                    moves_used=counters.moves_used,
                    consecutive_invalid=counters.consecutive_invalid,
                    moves_budget=self._config.limits.moves_budget_per_side,
                    consecutive_invalid_limit=self._config.limits.consecutive_invalid_limit,
                )
                if post_stop is not None:
                    self._write_shared_termination(txn, nid, doc, post_stop)
                    end_reason_from_chain = post_stop

            txn.set(negotiation_ref, model_to_firestore(doc))
            return MoveResponse(
                version=doc.version,
                status=doc.status,
                valid=outcome.valid,
                error=outcome.error,
                end_reason=end_reason_from_chain,
            )

        return self._run_transaction(_txn, contention_error=MovePreconditionFailed)

    def _apply_move_by_kind(
        self,
        doc: NegotiationDocument,
        side: Side,
        other_side: Side,
        counters: SideCounters,
        request: MoveRequest,
        now: dt.datetime,
    ) -> _MoveOutcome:
        if request.move == "check":
            return self._do_check(doc, side, counters, request)
        if request.move == "propose":
            return self._do_propose(doc, side, other_side, counters, request)
        if request.move == "accept":
            return self._do_accept(doc, side, request)
        if request.move == "reject":
            return self._do_reject(doc, side, other_side, request)
        if request.move == "ask_principal":
            return self._do_ask_principal(doc, side, counters, request, now)
        if request.move == "end":
            return self._do_end(side)
        return self._do_invalid(side, request)  # request.move == "invalid"(MoveRequest で保証済み)

    def _own_policy(self, doc: NegotiationDocument, side: Side) -> Policy:
        assert doc.snapshots is not None
        return doc.snapshots.candidate if side == "candidate" else doc.snapshots.employer

    def _do_check(
        self, doc: NegotiationDocument, side: Side, counters: SideCounters, request: MoveRequest
    ) -> _MoveOutcome:
        # check(P): 評価回数が尽きていれば無効(evaluation_budget_exhausted)。それ以外は常に有効。
        if counters.evaluations_used >= self._config.limits.evaluation_budget_per_side:
            view = EventView(seq=0, kind="invalid", package=request.package, reason="evaluation_budget_exhausted")
            return _MoveOutcome(False, "evaluation_budget_exhausted", view, None, None)

        counters.evaluations_used += 1
        verdict = evaluate(self._own_policy(doc, side), request.package)
        evaluated = EvaluatedPackage(package=request.package, own_evaluation=verdict)
        if side == "candidate":
            doc.last_check.candidate = evaluated
        else:
            doc.last_check.employer = evaluated
        view = EventView(seq=0, kind="check", package=request.package, own_evaluation=verdict)
        return _MoveOutcome(True, None, view, None, None)

    def _do_propose(
        self,
        doc: NegotiationDocument,
        side: Side,
        other_side: Side,
        counters: SideCounters,
        request: MoveRequest,
    ) -> _MoveOutcome:
        if counters.evaluations_used >= self._config.limits.evaluation_budget_per_side:
            view = EventView(seq=0, kind="invalid", package=request.package, reason="evaluation_budget_exhausted")
            return _MoveOutcome(False, "evaluation_budget_exhausted", view, None, None)

        counters.evaluations_used += 1  # ガードの評価(§3.5)
        own_verdict = evaluate(self._own_policy(doc, side), request.package)
        if own_verdict is not Verdict.ACCEPTABLE:
            view = EventView(
                seq=0, kind="invalid", package=request.package, reason="not_acceptable_to_own_principal"
            )
            return _MoveOutcome(False, "not_acceptable_to_own_principal", view, None, None)

        # 受け手としての評価(数えない。§3.5)
        receiver_verdict = evaluate(self._own_policy(doc, other_side), request.package)
        doc.pending_offer = PendingOffer(by=side, package=request.package, receiver_evaluation=receiver_verdict)
        doc.to_move = other_side
        mover_view = EventView(seq=0, kind="propose", package=request.package)
        counterparty_view = EventView(
            seq=0, kind="offer_received", package=request.package, own_evaluation=receiver_verdict
        )
        return _MoveOutcome(True, None, mover_view, counterparty_view, None)

    def _do_accept(self, doc: NegotiationDocument, side: Side, request: MoveRequest) -> _MoveOutcome:
        if doc.pending_offer is None:
            view = EventView(seq=0, kind="invalid", reason="no_pending_offer")
            return _MoveOutcome(False, "no_pending_offer", view, None, None)

        package = doc.pending_offer.package
        # 自分側の評価の確かめ直し(数えない。§3.5)
        own_verdict = evaluate(self._own_policy(doc, side), package)
        if own_verdict is not Verdict.ACCEPTABLE:
            view = EventView(
                seq=0, kind="invalid", package=package, reason="not_acceptable_to_own_principal"
            )
            return _MoveOutcome(False, "not_acceptable_to_own_principal", view, None, None)

        # §3.6: 合意と同じトランザクションで最終判定まで行う。
        assert doc.snapshots is not None
        result = judge(doc.snapshots.candidate, doc.snapshots.employer, package, self._config.t_high)
        candidate_view = EventView(seq=0, kind="final_result", result=result)
        employer_view = EventView(seq=0, kind="final_result", result=result)
        mover_view = candidate_view if side == "candidate" else employer_view
        counterparty_view = employer_view if side == "candidate" else candidate_view
        return _MoveOutcome(True, None, mover_view, counterparty_view, ("agreed", result))

    def _do_reject(
        self, doc: NegotiationDocument, side: Side, other_side: Side, request: MoveRequest
    ) -> _MoveOutcome:
        if doc.pending_offer is None:
            view = EventView(seq=0, kind="invalid", reason="no_pending_offer")
            return _MoveOutcome(False, "no_pending_offer", view, None, None)

        package = doc.pending_offer.package
        doc.pending_offer = None
        doc.to_move = other_side
        mover_view = EventView(seq=0, kind="reject", package=package)
        counterparty_view = EventView(seq=0, kind="offer_rejected", package=package)
        return _MoveOutcome(True, None, mover_view, counterparty_view, None)

    def _do_ask_principal(
        self,
        doc: NegotiationDocument,
        side: Side,
        counters: SideCounters,
        request: MoveRequest,
        now: dt.datetime,
    ) -> _MoveOutcome:
        if counters.evaluations_used >= self._config.limits.evaluation_budget_per_side:
            view = EventView(seq=0, kind="invalid", package=request.package, reason="evaluation_budget_exhausted")
            return _MoveOutcome(False, "evaluation_budget_exhausted", view, None, None)

        # 評価は必ず行う(NEEDS_CONFIRMATION かどうかを知るのに要るため。§3.5 の表の読み方は
        # 報告の 4 に記載: question_budget_exhausted の分岐でも評価は消費する)。
        counters.evaluations_used += 1
        verdict = evaluate(self._own_policy(doc, side), request.package)
        if verdict is not Verdict.NEEDS_CONFIRMATION:
            view = EventView(seq=0, kind="invalid", package=request.package, reason="question_not_applicable")
            return _MoveOutcome(False, "question_not_applicable", view, None, None)

        if counters.principal_checks_used >= self._config.limits.principal_checks_per_side:
            view = EventView(seq=0, kind="invalid", package=request.package, reason="question_budget_exhausted")
            return _MoveOutcome(False, "question_budget_exhausted", view, None, None)

        counters.principal_checks_used += 1
        doc.status = "awaiting_principal"
        doc.pending_question = PendingQuestion(side=side, package=request.package)
        check_deadline = dt.timedelta(seconds=self._config.deadlines.principal_check_deadline_seconds)
        doc.deadline = min(now + check_deadline, doc.expires_at)
        view = EventView(seq=0, kind="ask_principal", package=request.package)
        return _MoveOutcome(True, None, view, None, None)

    def _do_end(self, side: Side) -> _MoveOutcome:
        result = NegotiationResult(likelihood="none", package=None)
        candidate_view = EventView(seq=0, kind="final_result", result=result)
        employer_view = EventView(seq=0, kind="final_result", result=result)
        mover_view = candidate_view if side == "candidate" else employer_view
        counterparty_view = employer_view if side == "candidate" else candidate_view
        return _MoveOutcome(True, None, mover_view, counterparty_view, ("ended_by_agent", result))

    def _do_invalid(self, side: Side, request: MoveRequest) -> _MoveOutcome:
        # §3.5 最終行: レフェリーが見つけた無効手(スキーマ違反・タイムアウト・A2A のエラー)の登録。
        view = EventView(seq=0, kind="invalid", package=request.package, reason=request.reason)
        return _MoveOutcome(False, request.reason, view, None, None)

    # ------------------------------------------------------------------
    # §4.4 途中確認の回答(principal-answer)
    # ------------------------------------------------------------------

    def process_principal_answer(self, nid: str, request: PrincipalAnswerRequest) -> PrincipalAnswerResponse:
        """POST .../principal-answer(§3.3・§4.4)。手の操作と同じ扱いの前提を、1 つの
        トランザクションで確かめてから、追記・評価し直し・状態と記録を行う。
        """
        negotiation_ref = self._negotiation_ref(nid)

        def _txn(txn: firestore.Transaction) -> PrincipalAnswerResponse:
            snap = negotiation_ref.get(transaction=txn)
            if not snap.exists:
                raise NotFoundError(nid)
            doc = model_from_firestore(NegotiationDocument, snap.to_dict())
            now = self._clock.now()

            # §3.4: 期限切れの判定は、トランザクションの最初で行う(手の操作と同じ)。
            if doc.status != "judged" and self._is_expired(doc, now):
                self._write_shared_termination(txn, nid, doc, "timeout")
                txn.set(negotiation_ref, model_to_firestore(doc))
                return PrincipalAnswerResponse(version=doc.version, status=doc.status, end_reason="timeout")

            # --- 前提。1 つでも崩れていれば 409、何も消費しない(手の操作と同じ扱い) ---
            if doc.version != request.expected_version:
                raise MovePreconditionFailed(
                    f"expected_version mismatch: have {doc.version}, got {request.expected_version}"
                )
            if (
                doc.status != "awaiting_principal"
                or doc.pending_question is None
                or doc.pending_question.side != request.side
                or doc.pending_question.package != request.package
            ):
                raise MovePreconditionFailed("no pending_question matches this principal-answer")

            side = request.side
            participant = doc.participants.candidate if side == "candidate" else doc.participants.employer

            # --- 削除中の依頼者(本物だけが対象)は拒否する ---
            principal_ref = None
            principal_data: dict = {}
            if not participant.is_fictional:
                assert participant.principal_id is not None
                principal_ref = self._principal_ref(participant.principal_id)
                principal_snap = principal_ref.get(transaction=txn)
                principal_data = principal_snap.to_dict() if principal_snap.exists else {}
                if principal_data.get("deleting", False):
                    raise MovePreconditionFailed("principal is being deleted")

            anchor = principal_answer.anchor_from_package(request.package)
            answer = request.answer

            # --- 1. 追記。交渉用コピーには常に、本体には本物かつ中立なときだけ(§4.4) ---
            assert doc.snapshots is not None
            own_copy_policy = doc.snapshots.candidate if side == "candidate" else doc.snapshots.employer
            new_copy_policy, _ = principal_answer.append_anchor_if_consistent(own_copy_policy, anchor, answer)
            if side == "candidate":
                doc.snapshots.candidate = new_copy_policy
            else:
                doc.snapshots.employer = new_copy_policy

            if not participant.is_fictional:
                removed_axes = list(principal_data.get("removed_axes", []))
                is_neutral = principal_answer.is_neutral_for_all_removed_axes(anchor, removed_axes, side, answer)
                if is_neutral and principal_data.get("policy") is not None:
                    body_policy = model_from_firestore(Policy, principal_data["policy"])
                    new_body_policy, _ = principal_answer.append_anchor_if_consistent(body_policy, anchor, answer)
                    txn.set(principal_ref, {"policy": model_to_firestore(new_body_policy)}, merge=True)

            # --- 2. 評価し直し(答えた側だけ。評価回数には数えない) ---
            if doc.pending_offer is not None and doc.pending_offer.by != side:
                doc.pending_offer.receiver_evaluation = evaluate(new_copy_policy, doc.pending_offer.package)

            own_last_check = doc.last_check.candidate if side == "candidate" else doc.last_check.employer
            if own_last_check is not None:
                recomputed = EvaluatedPackage(
                    package=own_last_check.package,
                    own_evaluation=evaluate(new_copy_policy, own_last_check.package),
                )
                if side == "candidate":
                    doc.last_check.candidate = recomputed
                else:
                    doc.last_check.employer = recomputed

            # --- 3. status=active に戻し、期限を付け直し、答えた側の見え方にだけ記録する ---
            doc.status = "active"
            doc.pending_question = None
            # 一時停止中は期限を進めない(§3.4 の不変条件: paused のときだけ deadline は
            # null。control.pause/resume と同じ扱い)。
            doc.deadline = None if doc.paused else self._fresh_deadline(doc, now)

            own_evaluation_of_p = evaluate(new_copy_policy, request.package)
            view = EventView(
                seq=0,
                kind="principal_answer",
                package=request.package,
                own_evaluation=own_evaluation_of_p,
                answer=answer,
            )
            self._assign_seq(doc, side, view)
            candidate_view = view if side == "candidate" else None
            employer_view = view if side == "employer" else None
            self._record_event(txn, nid, doc, candidate_view, employer_view)

            txn.set(negotiation_ref, model_to_firestore(doc))
            return PrincipalAnswerResponse(version=doc.version, status=doc.status, end_reason=None)

        return self._run_transaction(_txn, contention_error=MovePreconditionFailed)

    # ------------------------------------------------------------------
    # §3.4 control(一時停止・再開・取消)・expire
    # ------------------------------------------------------------------

    def _fresh_deadline(self, doc: NegotiationDocument, now: dt.datetime) -> dt.datetime | None:
        if doc.status == "active":
            delta = dt.timedelta(seconds=self._config.deadlines.move_deadline_seconds)
        elif doc.status == "awaiting_principal":
            delta = dt.timedelta(seconds=self._config.deadlines.principal_check_deadline_seconds)
        else:
            return None
        return min(now + delta, doc.expires_at)

    def control(
        self, nid: str, request: ControlRequest, *, _bypass_deleting_guard: bool = False
    ) -> ControlResponse:
        """§3.4 の pause・resume・cancel。

        _bypass_deleting_guard は delete_principal の手順 2 専用(公開 API からは渡さない)。
        delete_principal は、まさに deleting にした依頼者自身の交渉を取消にする必要があるので、
        通常の削除中ガード(差し戻し対応 3)にここで弾かれては手順が進まなくなってしまう。
        """
        negotiation_ref = self._negotiation_ref(nid)

        def _txn(txn: firestore.Transaction) -> ControlResponse:
            snap = negotiation_ref.get(transaction=txn)
            if not snap.exists:
                raise NotFoundError(nid)
            doc = model_from_firestore(NegotiationDocument, snap.to_dict())
            now = self._clock.now()

            # §3.4: control は期限切れの判定より先に処理する(expired チェックをしない)。
            if doc.status == "judged":
                return ControlResponse(version=doc.version, status=doc.status, paused=doc.paused)

            if not _bypass_deleting_guard:
                self._reject_if_any_participant_deleting(doc, nid, txn)

            if request.action == "pause":
                if doc.paused:
                    return ControlResponse(version=doc.version, status=doc.status, paused=doc.paused)
                doc.paused = True
                doc.deadline = None
                doc.paused_at = now
                view = EventView(seq=0, kind="pause")
                self._assign_seq(doc, request.side, view)
                candidate_view = view if request.side == "candidate" else None
                employer_view = view if request.side == "employer" else None
                self._record_event(txn, nid, doc, candidate_view, employer_view)
                txn.set(negotiation_ref, model_to_firestore(doc))
                return ControlResponse(version=doc.version, status=doc.status, paused=doc.paused)

            if request.action == "resume":
                if not doc.paused:
                    return ControlResponse(version=doc.version, status=doc.status, paused=doc.paused)
                doc.paused = False
                doc.paused_at = None
                doc.deadline = self._fresh_deadline(doc, now)
                view = EventView(seq=0, kind="resume")
                self._assign_seq(doc, request.side, view)
                candidate_view = view if request.side == "candidate" else None
                employer_view = view if request.side == "employer" else None
                self._record_event(txn, nid, doc, candidate_view, employer_view)
                txn.set(negotiation_ref, model_to_firestore(doc))
                return ControlResponse(version=doc.version, status=doc.status, paused=doc.paused)

            # cancel: judged でなければ、どの状態からでも効く。
            self._write_shared_termination(txn, nid, doc, "cancelled")
            txn.set(negotiation_ref, model_to_firestore(doc))
            return ControlResponse(version=doc.version, status=doc.status, paused=doc.paused)

        return self._run_transaction(_txn, contention_error=TransactionRetryExhausted)

    def expire(self, nid: str) -> ExpireResponse:
        negotiation_ref = self._negotiation_ref(nid)

        def _txn(txn: firestore.Transaction) -> ExpireResponse:
            snap = negotiation_ref.get(transaction=txn)
            if not snap.exists:
                raise NotFoundError(nid)
            doc = model_from_firestore(NegotiationDocument, snap.to_dict())
            now = self._clock.now()

            if doc.status == "judged" or not self._is_expired(doc, now):
                return ExpireResponse(version=doc.version, status=doc.status, expired=False)

            self._write_shared_termination(txn, nid, doc, "timeout")
            txn.set(negotiation_ref, model_to_firestore(doc))
            return ExpireResponse(version=doc.version, status=doc.status, expired=True)

        return self._run_transaction(_txn, contention_error=TransactionRetryExhausted)

    # ------------------------------------------------------------------
    # §3.3 view・events・一覧
    # ------------------------------------------------------------------

    def get_view(self, nid: str, side: Side) -> NegotiationViewResponse:
        snap = self._negotiation_ref(nid).get()
        if not snap.exists:
            raise NotFoundError(nid)
        doc = model_from_firestore(NegotiationDocument, snap.to_dict())
        self._reject_if_side_participant_deleting(doc, side, nid)
        counters = doc.counters.candidate if side == "candidate" else doc.counters.employer

        pending_offer_view = None
        if doc.pending_offer is not None and doc.pending_offer.by != side:
            pending_offer_view = EvaluatedPackage(
                package=doc.pending_offer.package, own_evaluation=doc.pending_offer.receiver_evaluation
            )

        last_check = doc.last_check.candidate if side == "candidate" else doc.last_check.employer

        awaiting_package = None
        if doc.pending_question is not None and doc.pending_question.side == side:
            awaiting_package = doc.pending_question.package

        limits = self._config.limits
        response_budget = Budget(
            remaining_evaluations=max(0, limits.evaluation_budget_per_side - counters.evaluations_used),
            remaining_moves=max(0, limits.moves_budget_per_side - counters.moves_used),
            remaining_principal_checks=max(
                0, limits.principal_checks_per_side - counters.principal_checks_used
            ),
        )

        return NegotiationViewResponse(
            status=doc.status,
            to_move=doc.to_move,
            paused=doc.paused,
            pending_offer=pending_offer_view,
            last_check=last_check,
            awaiting_principal_package=awaiting_package,
            budget=response_budget,
            deadline=doc.deadline,
            expires_at=doc.expires_at,
            version=doc.version,
            result=doc.result,
        )

    def get_events(self, nid: str, side: Side, after_seq: int = 0) -> list[EventViewItem]:
        neg_snap = self._negotiation_ref(nid).get()
        if not neg_snap.exists:
            raise NotFoundError(nid)
        doc = model_from_firestore(NegotiationDocument, neg_snap.to_dict())
        self._reject_if_side_participant_deleting(doc, side, nid)

        field_path = f"views.{side}.seq"
        query = self._events(nid).where(filter=FieldFilter(field_path, ">", after_seq)).order_by(field_path)
        items: list[EventViewItem] = []
        for snap in query.stream():
            view_data = snap.to_dict()["views"][side]
            view = model_from_firestore(EventView, view_data)
            items.append(
                EventViewItem(
                    seq=view.seq,
                    kind=view.kind,
                    package=view.package,
                    own_evaluation=view.own_evaluation.value if view.own_evaluation is not None else None,
                    reason=view.reason,
                    answer=view.answer,
                    result=view.result,
                )
            )
        return items

    def list_principal_negotiations(self, pid: str) -> list[PrincipalNegotiationSummary]:
        """§3.3 GET /v1/principals/{pid}/negotiations。終了理由・相手の回数・version は返さない。"""
        self._reject_if_principal_deleting(pid)
        by_candidate = self._negotiations().where(
            filter=FieldFilter("participants.candidate.principal_id", "==", pid)
        )
        by_employer = self._negotiations().where(
            filter=FieldFilter("participants.employer.principal_id", "==", pid)
        )
        seen: dict[str, NegotiationDocument] = {}
        for snap in list(by_candidate.stream()) + list(by_employer.stream()):
            doc = model_from_firestore(NegotiationDocument, snap.to_dict())
            seen[doc.nid] = doc

        summaries = []
        for doc in seen.values():
            if doc.status == "judged":
                state = "ended"
            elif doc.paused:
                state = "paused"
            else:
                state = "active"
            summaries.append(
                PrincipalNegotiationSummary(
                    nid=doc.nid,
                    job_id=doc.participants.employer.job_id or "",
                    created_at=doc.created_at,
                    state=state,
                    result=doc.result if doc.status == "judged" else None,
                )
            )
        summaries.sort(key=lambda s: s.created_at)
        return summaries

    def list_open_negotiations(
        self, cursor: str | None = None, page_size: int = 50
    ) -> OpenNegotiationsPage:
        """§3.3 GET /v1/negotiations?open=true&cursor=。見回り用の一覧。

        Firestore の複合インデックスを避けるため、単一フィールドの等価フィルタ
        (status in [...])だけで絞り込み、並び替え・カーソルの位置決めは Python 側で行う。
        """
        query = self._negotiations().where(filter=FieldFilter("status", "in", ["active", "awaiting_principal"]))
        docs = [model_from_firestore(NegotiationDocument, snap.to_dict()) for snap in query.stream()]
        docs.sort(key=lambda d: d.nid)

        start_index = 0
        if cursor is not None:
            for i, doc in enumerate(docs):
                if doc.nid == cursor:
                    start_index = i + 1
                    break

        page = docs[start_index : start_index + page_size]
        next_cursor = page[-1].nid if len(docs) > start_index + page_size and page else None

        items = [
            OpenNegotiationSummary(
                nid=doc.nid,
                status=doc.status,
                paused=doc.paused,
                deadline=doc.deadline,
                expires_at=doc.expires_at,
            )
            for doc in page
        ]
        return OpenNegotiationsPage(items=items, next_cursor=next_cursor)

    # ------------------------------------------------------------------
    # §3.8 依頼者の削除
    # ------------------------------------------------------------------

    def _negotiation_ids_for_principal(self, pid: str) -> list[str]:
        """pid が候補者側・求人側のどちらかとして関わる交渉の nid の一覧(重複なし)。

        list_principal_negotiations と同じ 2 本の等価フィルタで探す。呼び出しのたびに
        Firestore を読み直す(削除の各段が冪等に、そのつどの最新状態に基づいて進むように)。
        """
        neg_col = self._negotiations()
        by_candidate = neg_col.where(filter=FieldFilter("participants.candidate.principal_id", "==", pid))
        by_employer = neg_col.where(filter=FieldFilter("participants.employer.principal_id", "==", pid))
        nids = {snap.id for snap in by_candidate.stream()}
        nids.update(snap.id for snap in by_employer.stream())
        return list(nids)

    def _delete_negotiation_events(self, nid: str) -> None:
        """交渉のイベント列を、カーソルで区切りながら明示的に消す(§3.8 手順 3)。

        limit() で区切って削除してから読み直す: 削除済みの分は次の読み出しに現れないので、
        コレクション全体を一度に読み込まずに済む(カーソルで区切るのと同じ効果になる)。
        """
        events_col = self._events(nid)
        while True:
            pending = list(events_col.limit(300).stream())
            if not pending:
                return
            batch = self._db.batch()
            for event_snap in pending:
                batch.delete(event_snap.reference)
            batch.commit()

    def _null_out_side_views(self, nid: str, side: Side) -> None:
        """イベント列の中の、side の見え方をすべて null にする(§3.8 手順 3)。

        すでに null な記録はそのままにする(冪等)。相手の見え方・seq には触れない。
        """
        field_path = f"views.{side}"
        batch = self._db.batch()
        pending = 0
        for event_snap in self._events(nid).stream():
            views = event_snap.to_dict().get("views", {})
            if views.get(side) is not None:
                batch.update(event_snap.reference, {field_path: None})
                pending += 1
                if pending >= 400:
                    batch.commit()
                    batch = self._db.batch()
                    pending = 0
        if pending:
            batch.commit()

    def _process_negotiation_for_deletion(self, pid: str, nid: str) -> None:
        """§3.8 手順 3。相手を見て、自分側の見え方を消すか、交渉を丸ごと消すか決める。"""
        negotiation_ref = self._negotiation_ref(nid)
        snap = negotiation_ref.get()
        if not snap.exists:
            return  # 冪等: すでに消えている

        doc = model_from_firestore(NegotiationDocument, snap.to_dict())
        if doc.participants.candidate.principal_id == pid:
            own_side: Side = "candidate"
            other_participant = doc.participants.employer
        elif doc.participants.employer.principal_id == pid:
            own_side = "employer"
            other_participant = doc.participants.candidate
        else:
            return  # 防御的: 通常は起こらない(この交渉は pid に関わっていない)

        if other_participant.is_fictional:
            self._delete_negotiation_events(nid)
            negotiation_ref.delete()
        else:
            self._null_out_side_views(nid, own_side)

    def delete_principal(self, pid: str) -> None:
        """DELETE /v1/principals/{pid}(§3.8)。各段は冪等。すでに消えていれば成功を返す。

        4 段(deleting にする・未終了の交渉を取消・交渉ごとの後始末・依頼者文書を消す)を
        順に行う。途中で例外が飛んでも、呼び直せば残りの段から続けられる(各段が冪等なため、
        すでに終わった段はそのつど no-op になる)。
        """
        principal_ref = self._principal_ref(pid)
        snap = principal_ref.get()
        if not snap.exists:
            return  # 冪等: そもそもデータがない(すでに消えている)

        # 1. deleting にする。以後、この依頼者が関わる操作(依頼者を単位にする PUT/GET
        #    policy・PUT blocklist・本人の交渉一覧・作成、交渉を単位にする moves・control・
        #    view・events)はすべて拒否される(差し戻し対応 3)。expire だけは例外(システムの
        #    操作なので)。
        if not snap.to_dict().get("deleting", False):
            principal_ref.set({"deleting": True}, merge=True)

        # 2. 関わる交渉のうち、終わっていないものに終了処理(cancelled)を行う。
        #    control の cancel 分岐は request.side を参照しないので(§3.4)、ここでは
        #    仮の値を渡す(どちらでも結果は変わらない)。まさに今 deleting にした依頼者自身の
        #    交渉を取消にする必要があるので、通常の削除中ガードは bypass する。
        for nid in self._negotiation_ids_for_principal(pid):
            self.control(nid, ControlRequest(side="candidate", action="cancel"), _bypass_deleting_guard=True)

        # 3. 関わる交渉ごとに、相手を見て後始末する。
        for nid in self._negotiation_ids_for_principal(pid):
            self._process_negotiation_for_deletion(pid, nid)

        # 4. 依頼者の文書を消す(ポリシー・外した軸・ブロックリスト・帯・評価予算すべて)。
        principal_ref.delete()
