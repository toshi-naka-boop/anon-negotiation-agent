"""面談の進行(design.md §5 の手順 1〜9)。API(web.interview.api)から呼ばれる。LLM のほかは、決定的なコード。

各段の状態は、サーバのメモリに依頼者 ID ごとに持つ(web.interview.state)。生の値は Firestore にも金庫にも書かない。金庫に書くのは、
最後の submit だけ(AC-01: 確認前に金庫への書き込みは 0 件)。submit は、送信のロジック submit_policy ── web.api_models.InterviewSubmitRequest で
丸める・利用記録 principals_meta を先に作る・金庫の PUT policy ── を呼ぶ。
丸める前のアンカーと属性帯を直接受ける HTTP の口(旧 POST /v1/principals/{pid}/interview)は、公開面から外した(台帳 X-81: 3 問・二択・確認・
最悪ここまでの承認を経ずに金庫へ書けてしまうため)。submit_policy は、HTTP には出さない内部の関数で、画面の流れを通さずに依頼者を作りたい
テストの補助(tests/web_app_helpers.py の submit_interview)だけが直接呼ぶ。

進める順番(手順の前提)は、サーバが確かめる。前の手順を終えていなければ 409(detail は、足りない手順の名前)。
- 年収の読み取りの前に、プロフィール(profile_missing)。年収の確認の前に、読み取り(salary_proposal_missing)。
- 軸を外す選択の前に、年収の確認(salary_not_confirmed)。二択・自由コメント・辞めた理由の前に、軸を外す選択(axes_not_chosen)。
- 確認画面(平文での確認)に進むには、年収の確認と、二択を最小の組数(5)以上答え終えること(choices_incomplete。AC-01)。
- 「最悪ここまで」の承認は、確認のあと(not_confirmed)。送信は、確認と承認がそろっていること(not_confirmed・worst_case_not_approved)。
  確認のあとで内容を変えれば(revision が進む)、確認し直しになる。
"""

from typing import Any

from fastapi import HTTPException

from negotiation_core import AXES, AXIS_KEYS
from vault.api_models import PutBlocklistRequest

from web.api_models import InterviewSubmitRequest
from web.config import LimitsConfig
from web.interview import agent as agent_module
from web.interview.agent import ExtractionKind, InterviewAgent, InterviewLlmFailure
from web.interview.anchors import (
    AnchorEntry,
    ChoiceAnswer,
    DerivedEntries,
    StatementRecord,
    count_by_polarity,
    derive_entries,
    find_conflicts,
    to_submit_request,
    worst_case_view,
)
from web.interview.choices import ChoicePair, generate_pairs, removed_axes_question
from web.interview.config import InterviewConfig
from web.interview.profile import ProfileError, profile_to_bands
from web.interview.salary import NormalizedSalary, SalaryBasis, SalaryConversionError, nearest_grid_index, normalize_salary
from web.interview.sentences import describe_anchor, describe_offer, describe_statement, display_value
from web.interview.state import (
    InterviewClientLimitReached,
    InterviewState,
    InterviewStateStore,
    InterviewStoreFull,
    SalaryProposal,
)
from web.interview.statements import choice_not_saved_reason, statement_skip_reason
from web.interview.templates import DISCRETE_AXIS_KEYS, InterviewTemplates
from web.limits import INTERVIEW_CONCURRENT_ENTRANCE, rate_limited_error
from web.llm_budget import LlmBudgetUnavailable
from web.principals_meta import PrincipalsMetaStore
from web.vault_client import VaultClient

_LLM_STATUS = {
    agent_module.DAILY_LIMIT_REACHED: 429,
    agent_module.LLM_UNAVAILABLE: 503,
    agent_module.LLM_FAILED: 502,
    agent_module.OUTPUT_TRUNCATED: 502,
    agent_module.OUTPUT_INVALID: 502,
}


class InterviewError(HTTPException):
    """面談の API が返すエラー。detail は理由の名前(入力の値は含めない)。"""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(status_code=status_code, detail=code)


def _llm_error(failure: InterviewLlmFailure) -> InterviewError:
    return InterviewError(_LLM_STATUS[failure.code], failure.code)


class InterviewService:
    """面談の進行。状態は state(メモリ)、LLM は agent、金庫への書き込みは submit だけ。"""

    def __init__(
        self,
        *,
        vault: VaultClient,
        meta: PrincipalsMetaStore,
        agent: InterviewAgent,
        store: InterviewStateStore,
        templates: InterviewTemplates,
        config: InterviewConfig,
        limits: LimitsConfig,
        retention_days: int,
        companies: list[dict[str, str]],
    ) -> None:
        self._vault = vault
        self._meta = meta
        self.agent = agent
        self.store = store
        self._templates = templates
        self._config = config
        self._limits = limits
        self._retention_days = retention_days
        self._companies = companies

    @property
    def max_body_bytes(self) -> int:
        """面談の LLM に向かう入力を含む、リクエスト本文の上限(バイト。§5・台帳 C-49)。"""
        return self._config.max_request_body_bytes

    # ------------------------------------------------------------------
    # 状態の取り出しと、手順の前提の確認
    # ------------------------------------------------------------------

    def _state(self, principal_id: str, *, touch: bool = True) -> InterviewState:
        """面談の状態(なければ 409)。書き込みの手順は、既定の touch=True で、アイドルの時計を進め直す。
        読み取り(GET)は touch=False で呼ぶ: 読んでも、アイドルの寿命は延びない(台帳 C-69・X-87)。
        """
        state = self.store.get(principal_id, touch=touch)
        if state is None:
            raise InterviewError(409, "interview_not_started")
        return state

    @staticmethod
    def _removed(state: InterviewState) -> tuple[str, ...]:
        return state.removed_axes or ()

    def _pairs(self, state: InterviewState) -> list[ChoicePair]:
        """二択の組(年収の確認と、軸を外す選択を終えているときだけ)。"""
        if state.salary is None:
            raise InterviewError(409, "salary_not_confirmed")
        if state.removed_axes is None:
            raise InterviewError(409, "axes_not_chosen")
        return generate_pairs(state.salary.man_yen, state.removed_axes, self._templates, self._config.choice_pairs)

    @staticmethod
    def _answered_pairs(state: InterviewState, pairs: list[ChoicePair]) -> int:
        return sum(1 for pair in pairs if (pair.id, "a") in state.answers and (pair.id, "b") in state.answers)

    def _derived(self, state: InterviewState) -> DerivedEntries:
        return derive_entries(state.answers, state.statements, state.inactive, self._removed(state))

    def _require_ready_for_confirmation(self, state: InterviewState) -> DerivedEntries:
        """確認画面に進む前提: 年収の確認・軸を外す選択・二択を最小の組数以上(AC-01)。アンカーの一覧を返す。"""
        pairs = self._pairs(state)
        if self._answered_pairs(state, pairs) < self._config.min_answered_pairs:
            raise InterviewError(409, "choices_incomplete")
        return self._derived(state)

    def _reset_choices(self, state: InterviewState) -> None:
        """二択からやり直す(§2.4: 軸を外す選択を変えたとき。年収の土台が変わったとき)。"""
        state.answers.clear()
        state.inactive = {key for key in state.inactive if not key.startswith("choice-")}
        state.bump()

    # ------------------------------------------------------------------
    # 見え方
    # ------------------------------------------------------------------

    def notice(self) -> dict[str, Any]:
        """面談の入口の注記(Vertex AI 側の記録・global エンドポイント・30 日で自動削除。§5 の末尾・§1.2・§6.3)。"""
        notice = self._templates.notice
        return {
            "title": notice.title,
            "items": [
                {"id": "vertex_ai", "text": notice.vertex_ai},
                {"id": "global_endpoint", "text": notice.global_endpoint},
                {"id": "server_memory", "text": notice.server_memory},
                {"id": "auto_delete", "text": notice.auto_delete.format(days=self._retention_days)},
            ],
        }

    def texts(self) -> dict[str, Any]:
        """画面がそのまま出す、設問・選択肢の文面(暫定。U-01)。"""
        templates = self._templates
        return {
            "provisional": templates.provisional,
            "salary_questions": list(templates.salary_questions),
            "free_comment_prompt": templates.free_comment.prompt,
            "reason_prompt": templates.reason_for_leaving.prompt,
            "axes": {"notice": templates.axes.notice, "remove_label": templates.axes.remove_label},
            "choices": {
                "intro": templates.two_choice.intro,
                "answers": templates.two_choice.answers.model_dump(),
                "min_answered_pairs": self._config.min_answered_pairs,
            },
            "profile": {
                "experience_bands": [
                    {"key": key, "label": label} for key, label in templates.labels.experience_band.items()
                ],
                "regions": [
                    {"block": block, "label": templates.labels.region_block[block], "prefectures": prefectures}
                    for block, prefectures in templates.region_prefectures.items()
                ],
                "job_categories": [
                    {"key": key, "label": label} for key, label in templates.labels.job_category.items()
                ],
            },
        }

    def _stage(self, state: InterviewState, answered: int) -> str:
        if state.bands is None:
            return "profile"
        if state.salary is None:
            return "salary"
        if state.removed_axes is None:
            return "axes"
        if answered < self._config.min_answered_pairs:
            return "choices"
        if state.confirmed_revision != state.revision:
            return "confirm"
        if state.worst_case_revision != state.revision:
            return "worst_case"
        return "ready"

    def view(self, principal_id: str) -> dict[str, Any]:
        return self._view(self._state(principal_id, touch=False))

    def _view(self, state: InterviewState) -> dict[str, Any]:
        pairs = self._pairs(state) if state.salary is not None and state.removed_axes is not None else []
        answered = self._answered_pairs(state, pairs)
        derived = self._derived(state)
        counts = count_by_polarity(derived.entries)
        return {
            "stage": self._stage(state, answered),
            "revision": state.revision,
            "bands": state.bands.model_dump() if state.bands is not None else None,
            "salary": {"proposed": state.salary_proposal is not None, "confirmed": state.salary is not None},
            "removed_axes": list(state.removed_axes) if state.removed_axes is not None else None,
            "choices": {
                "total": len(pairs),
                "answered": answered,
                "required": self._config.min_answered_pairs,
            },
            "entries": {**counts, "inactive": sum(1 for entry in derived.entries if not entry.active)},
            "confirmed": state.confirmed_revision == state.revision,
            "worst_case_approved": state.worst_case_revision == state.revision,
            "blocklist": list(state.blocklist) if state.blocklist is not None else None,
        }

    # ------------------------------------------------------------------
    # 手順 0: 始める(入口の注記を出す)
    # ------------------------------------------------------------------

    def begin(self, principal_id: str, restart: bool, client: str) -> dict[str, Any]:
        """面談を始める(すでに途中のものがあれば、restart でなければそのまま続ける)。最初の応答に、入口の注記を含める。

        client は、要求を送ってきた送信元のキー(web.client_ip.client_key。IPv6 は /64 単位)。続きを読み込むだけの begin は、書き込み(アイドルの時計を進め直す)だが、
        同時数には数えない。新しく状態を作る begin(やり直しを含む)は、この送信元が、置き換えるもの以外に max_concurrent_per_client 件持っていれば
        429(入口 interview_concurrent。台帳 C-69・X-87)。全体の上限なら 503。
        """
        state = self.store.get(principal_id, touch=True)
        if state is None or restart:
            try:
                state = self.store.create(principal_id, client)
            except InterviewClientLimitReached as reached:
                raise rate_limited_error(
                    INTERVIEW_CONCURRENT_ENTRANCE,
                    "client",
                    limit=reached.limit,
                    window_seconds=None,
                    retry_after_seconds=reached.retry_after_seconds,
                ) from None
            except InterviewStoreFull:
                raise InterviewError(503, "too_many_interviews") from None
        return {"notice": self.notice(), "texts": self.texts(), "state": self._view(state)}

    def discard(self, principal_id: str) -> dict[str, str]:
        """面談を破棄する(途中の状態をメモリから消す)。"""
        self.store.discard(principal_id)
        return {"status": "discarded"}

    # ------------------------------------------------------------------
    # 手順 1: プロフィール
    # ------------------------------------------------------------------

    def set_profile(self, principal_id: str, experience_years: float, prefecture: str, job_category: str) -> dict[str, Any]:
        """正確な値をその場で帯に変換して、帯だけを覚える(§2.6。正確な値は捨てる)。"""
        state = self._state(principal_id)
        try:
            state.bands = profile_to_bands(
                experience_years=experience_years,
                prefecture=prefecture,
                job=job_category,
                upper_bounds=self._config.experience_band_upper_bounds,
                region_prefectures=self._templates.region_prefectures,
            )
        except ProfileError as exc:
            raise InterviewError(422, exc.code) from None
        return self._view(state)

    # ------------------------------------------------------------------
    # 手順 2: 年収の正規化
    # ------------------------------------------------------------------

    def _salary_proposal_view(self, proposal: SalaryProposal) -> dict[str, Any]:
        return {
            "salary_basis": proposal.basis.model_dump(),
            "normalized_man_yen": proposal.normalized.man_yen,
            "formula": proposal.normalized.formula,
            "assumptions": list(proposal.normalized.assumptions),
        }

    def _normalize(self, basis: SalaryBasis) -> NormalizedSalary:
        try:
            return normalize_salary(basis, self._config.net_to_gross_ratio)
        except SalaryConversionError:
            raise InterviewError(422, "salary_basis_invalid") from None

    async def propose_salary(self, principal_id: str, answers: list[str]) -> dict[str, Any]:
        """3 問の回答を面談エージェントで読み取り、比較基準年収への換算の式と前提を返す(本人が確かめる)。回答の原文は覚えない。"""
        if self._state(principal_id).bands is None:
            raise InterviewError(409, "profile_missing")
        questions = self._templates.salary_questions
        try:
            basis = await self.agent.extract_salary_basis(list(zip(questions, answers, strict=True)))
        except InterviewLlmFailure as failure:
            raise _llm_error(failure) from None
        except LlmBudgetUnavailable:
            raise InterviewError(503, "temporarily_unavailable") from None
        state = self._state(principal_id)  # LLM を待っている間に、状態が消えていないことを確かめ直す
        state.salary_proposal = SalaryProposal(basis=basis, normalized=self._normalize(basis))
        return self._salary_proposal_view(state.salary_proposal)

    def confirm_salary(self, principal_id: str, basis: SalaryBasis) -> dict[str, Any]:
        """本人が確かめた(直したかもしれない)年収の定義を、決定的に換算して覚える。二択の土台の年収が変われば、二択からやり直す。"""
        state = self._state(principal_id)
        if state.salary_proposal is None:
            raise InterviewError(409, "salary_proposal_missing")
        normalized = self._normalize(basis)
        previous = state.salary
        state.salary = normalized
        state.salary_proposal = SalaryProposal(basis=basis, normalized=normalized)
        if previous is not None and state.answers and nearest_grid_index(previous.man_yen) != nearest_grid_index(normalized.man_yen):
            self._reset_choices(state)
        else:
            state.bump()
        return {**self._salary_proposal_view(state.salary_proposal), "state": self._view(state)}

    # ------------------------------------------------------------------
    # 手順 3: 軸を外すかどうか
    # ------------------------------------------------------------------

    def axes_view(self, principal_id: str) -> dict[str, Any]:
        state = self._state(principal_id, touch=False)
        removed = self._removed(state)
        return {
            "notice": self._templates.axes.notice,
            "remove_label": self._templates.axes.remove_label,
            "axes": [{"axis": axis, "label": AXES[axis].label, "removed": axis in removed} for axis in DISCRETE_AXIS_KEYS],
            "chosen": state.removed_axes is not None,
        }

    def set_axes(self, principal_id: str, removed_axes: list[str]) -> dict[str, Any]:
        """外す軸を決める。変えたときは、二択からやり直す(§2.4: 二択の後で外したくなったら、二択からやり直す)。"""
        state = self._state(principal_id)
        if state.salary is None:
            raise InterviewError(409, "salary_not_confirmed")
        chosen = tuple(axis for axis in AXIS_KEYS if axis in removed_axes)
        changed = state.removed_axes is not None and set(state.removed_axes) != set(chosen)
        state.removed_axes = chosen
        choices_reset = changed and bool(state.answers)
        if choices_reset:
            self._reset_choices(state)
        else:
            state.bump()
        return {"removed_axes": list(chosen), "choices_reset": choices_reset, "state": self._view(state)}

    # ------------------------------------------------------------------
    # 手順 4: パッケージ二択・自由コメント
    # ------------------------------------------------------------------

    def _pair_view(self, state: InterviewState, pair: ChoicePair) -> dict[str, Any]:
        removed = self._removed(state)
        options = {}
        for name, values in (("a", pair.a), ("b", pair.b)):
            answer = state.answers.get((pair.id, name))
            options[name] = {
                "text": describe_offer(values, removed),
                "axes": [
                    {
                        "axis": axis,
                        "label": AXES[axis].label,
                        "value": display_value(axis, values[axis]),
                        "removed": axis in removed,
                    }
                    for axis in AXIS_KEYS
                ],
                "answer": answer.response if answer is not None else None,
            }
        return {"id": pair.id, "question": removed_axes_question(removed, self._templates), "options": options}

    def choices_view(self, principal_id: str) -> dict[str, Any]:
        state = self._state(principal_id, touch=False)
        pairs = self._pairs(state)
        return {
            "intro": self._templates.two_choice.intro,
            "answers": self._templates.two_choice.answers.model_dump(),
            "pairs": [self._pair_view(state, pair) for pair in pairs],
            "answered": self._answered_pairs(state, pairs),
            "required": self._config.min_answered_pairs,
        }

    def answer_choice(self, principal_id: str, pair_id: str, option: str, response: str) -> dict[str, Any]:
        """二択への回答を覚える(やり直しで上書きできる)。外した軸があるときの「行かない」は、アンカーにしない(§2.4)。"""
        state = self._state(principal_id)
        pairs = self._pairs(state)
        pair = next((candidate for candidate in pairs if candidate.id == pair_id), None)
        if pair is None:
            raise InterviewError(422, "unknown_pair")
        state.answers[(pair_id, option)] = ChoiceAnswer(values=dict(pair.a if option == "a" else pair.b), response=response)
        state.bump()
        reason = choice_not_saved_reason(response, self._removed(state))
        return {
            "anchor_saved": response != "undecided" and reason is None,
            "reason": reason,
            "message": self._templates.two_choice.not_saved if reason is not None else None,
            "answered": self._answered_pairs(state, pairs),
            "required": self._config.min_answered_pairs,
        }

    async def add_statements(self, principal_id: str, kind: ExtractionKind, text: str) -> dict[str, Any]:
        """自由コメント(free_comment)・辞めた理由(reason_for_leaving)を面談エージェントで発言単位に構造化して、覚える。

        原文は覚えない(取り出した発言だけを持つ)。外した軸に触れる発言は保存しない(§2.4)。アンカーへの変換は、確認画面のたびに、
        決定的なコードが §2.3・§2.4 の規則で行う。
        """
        if self._state(principal_id).removed_axes is None:
            raise InterviewError(409, "axes_not_chosen")
        try:
            constraints, dropped = await self.agent.extract_constraints(kind, text)
        except InterviewLlmFailure as failure:
            raise _llm_error(failure) from None
        except LlmBudgetUnavailable:
            raise InterviewError(503, "temporarily_unavailable") from None
        state = self._state(principal_id)
        number = state.extractions + 1
        source = "comment" if kind == "free_comment" else "reason"
        records = [
            StatementRecord(key=f"{source}-{number}-{index}", source=source, statement=statement)
            for index, statement in enumerate(constraints.statements)
        ]
        derived = derive_entries(state.answers, [*state.statements, *records], state.inactive, self._removed(state))
        limit = self._limits.max_anchors_per_kind  # 送信の上限(web.api_models.InterviewSubmitRequest)と同じ。メモリの上限も兼ねる
        if len(state.statements) + len(records) > 2 * limit or any(
            sum(1 for entry in derived.entries if entry.polarity == polarity) > limit for polarity in ("accept", "reject")
        ):
            raise InterviewError(409, "too_many_anchors")
        state.statements.extend(records)
        state.extractions = number
        state.bump()
        removed = self._removed(state)
        results = []
        for record in records:
            reason = statement_skip_reason(record.statement, removed)
            results.append(
                {
                    "key": record.key,
                    "sentence": describe_statement(record.statement),
                    "saved": reason is None,
                    "reason": reason,
                    "message": self._templates.two_choice.not_saved if reason == "removed_axis" else None,
                }
            )
        return {"statements": results, "dropped": dropped}

    # ------------------------------------------------------------------
    # 手順 6: 平文での確認
    # ------------------------------------------------------------------

    def _entry_view(self, entry: AnchorEntry, removed: tuple[str, ...]) -> dict[str, Any]:
        return {
            "key": entry.key,
            "source": entry.source,
            "polarity": entry.polarity,
            "sentence": describe_anchor(entry.polarity, entry.raw, removed),
            "active": entry.active,
        }

    def _warnings(self, derived: DerivedEntries, removed: tuple[str, ...]) -> list[dict[str, Any]]:
        """受けるアンカーが 0 件のときの警告(I-1。外した軸が原因で起きる。§2.4・§5 の 6)。"""
        if count_by_polarity(derived.entries)["accept"] > 0:
            return []
        message = (
            "この条件を外すと、事前に受けられる組み合わせがなくなります。外すのをやめて二択からやり直すか、そのまま進むかを選んでください。"
            if removed
            else "事前に受けられる組み合わせがありません。そのまま進むと、交渉中の途中確認だけが頼りになります。"
        )
        return [
            {
                "code": "no_accept_anchors",
                "caused_by_removed_axes": bool(removed),
                "message": message,
                "options": ["restart_choices", "proceed"],
            }
        ]

    def confirmation(self, principal_id: str) -> dict[str, Any]:
        """確認画面: アンカーを平文にして見せる(埋めた値も含めて。FR-03)。二択を最小の組数以上答えるまでは出せない(AC-01)。"""
        state = self._state(principal_id, touch=False)
        derived = self._require_ready_for_confirmation(state)
        removed = self._removed(state)
        by_key = {entry.key: entry for entry in derived.entries}
        conflicts = find_conflicts(derived.entries)
        return {
            "entries": [self._entry_view(entry, removed) for entry in derived.entries],
            "not_saved": [
                {
                    "key": key,
                    "source": source,
                    "reason": "removed_axis",
                    "message": self._templates.two_choice.not_saved,
                }
                for key, source in derived.not_saved
            ],
            "ignored_statements": derived.ignored_statements,
            "conflicts": [
                {
                    "accept": accept_key,
                    "reject": reject_key,
                    "accept_sentence": describe_anchor("accept", by_key[accept_key].raw, removed),
                    "reject_sentence": describe_anchor("reject", by_key[reject_key].raw, removed),
                }
                for accept_key, reject_key in conflicts
            ],
            "warnings": self._warnings(derived, removed),
            "removed_axes": [{"axis": axis, "label": AXES[axis].label} for axis in removed],
            "confirmed": state.confirmed_revision == state.revision,
        }

    def set_entry_active(self, principal_id: str, key: str, active: bool) -> dict[str, Any]:
        """確認画面の項目を、消す・付け直す(§5 の 6)。消した項目は、一覧に残り、付け直せる。"""
        state = self._state(principal_id)
        derived = self._require_ready_for_confirmation(state)
        if all(entry.key != key for entry in derived.entries):
            raise InterviewError(404, "unknown_entry")
        if active:
            state.inactive.discard(key)
        else:
            state.inactive.add(key)
        state.bump()
        return self.confirmation(principal_id)

    def confirm(self, principal_id: str, proceed_without_accept_anchors: bool) -> dict[str, Any]:
        """本人が、平文のポリシーを確認した。矛盾があれば進めない。受けるアンカーが 0 件なら、そのまま進むことを選んだときだけ進める。"""
        state = self._state(principal_id)
        derived = self._require_ready_for_confirmation(state)
        if find_conflicts(derived.entries):
            raise InterviewError(409, "contradiction")
        if count_by_polarity(derived.entries)["accept"] == 0 and not proceed_without_accept_anchors:
            raise InterviewError(409, "no_accept_anchors")
        state.confirmed_revision = state.revision
        return self._view(state)

    # ------------------------------------------------------------------
    # 手順 7: 最悪ここまで
    # ------------------------------------------------------------------

    def _require_confirmed(self, state: InterviewState) -> DerivedEntries:
        derived = self._require_ready_for_confirmation(state)
        if state.confirmed_revision != state.revision:
            raise InterviewError(409, "not_confirmed")
        return derived

    def worst_case(self, principal_id: str) -> dict[str, Any]:
        """軸ごとの「最悪ここまで」: 丸めた後のマス。外した軸は「外しています(交渉中に確認)」。確認のあとに出せる。"""
        state = self._state(principal_id, touch=False)
        derived = self._require_confirmed(state)
        return {
            "axes": worst_case_view(derived.entries, self._removed(state)),
            "approved": state.worst_case_revision == state.revision,
        }

    def approve_worst_case(self, principal_id: str) -> dict[str, Any]:
        state = self._state(principal_id)
        self._require_confirmed(state)
        state.worst_case_revision = state.revision
        return self._view(state)

    # ------------------------------------------------------------------
    # 手順 8: ブロック先
    # ------------------------------------------------------------------

    def companies(self) -> dict[str, Any]:
        """全企業の一覧(求人があるかどうかは示さない)と、画面の説明(登録できるのは現職の企業だけ。P-1 の回答)。"""
        return {
            "prompt": self._templates.blocklist.prompt,
            "note": self._templates.blocklist.note,
            "companies": [dict(company) for company in self._companies],
        }

    def set_blocklist(self, principal_id: str, company_ids: list[str]) -> dict[str, Any]:
        """ブロック先の企業を覚える(送信のときに金庫へ置く)。一覧にない企業は断る。"""
        state = self._state(principal_id)
        known = {company["company_id"] for company in self._companies}
        unique = list(dict.fromkeys(company_ids))
        if any(company_id not in known for company_id in unique):
            raise InterviewError(422, "unknown_company")
        if len(unique) > self._limits.max_blocklist_entries:
            raise InterviewError(422, "too_many_companies")
        state.blocklist = unique
        return self._view(state)

    # ------------------------------------------------------------------
    # 手順 9: 送信
    # ------------------------------------------------------------------

    async def submit_policy(self, principal_id: str, submission: InterviewSubmitRequest) -> None:
        """送信のロジック(§5 の 9): 丸める(§2.5。矛盾は 422 policy_invalid)→ 利用記録を作る → 金庫の PUT policy。

        金庫に初めて書く前に、利用記録 principals_meta を作る(§5 の 9・§6.3。削除中の依頼者には 409 principal_deleting)。丸めに失敗したときは、
        利用記録も作らず、金庫にも書かない。面談の確認・「最悪ここまで」の承認は確かめない(submit が確かめてから呼ぶ)ので、HTTP には出さない
        (台帳 X-81)。画面の流れを通さずに依頼者を作るテストの補助だけが、直接呼ぶ。
        """
        try:
            put_policy_request = submission.to_put_policy_request()
        except ValueError:
            raise InterviewError(422, "policy_invalid") from None  # エラーの文には値が入るので返さない
        if await self._meta.create_if_absent(principal_id) == "deleting":
            raise InterviewError(409, "principal_deleting")
        await self._vault.put_policy(principal_id, put_policy_request)

    async def submit(self, principal_id: str) -> dict[str, str]:
        """確認と「最悪ここまで」の承認がそろった面談を、web で丸めて金庫に送る。送ったら、面談の状態をメモリから消す。"""
        state = self._state(principal_id)
        derived = self._require_confirmed(state)
        if state.worst_case_revision != state.revision:
            raise InterviewError(409, "worst_case_not_approved")
        if state.bands is None:
            raise InterviewError(409, "profile_missing")
        if find_conflicts(derived.entries):
            raise InterviewError(409, "contradiction")
        try:
            submission = to_submit_request(derived.entries, self._removed(state), state.bands)
        except ValueError:
            raise InterviewError(422, "policy_invalid") from None  # エラーの文には値が入るので返さない
        await self.submit_policy(principal_id, submission)
        if state.blocklist is not None:
            await self._vault.put_blocklist(principal_id, PutBlocklistRequest(blocklist=list(state.blocklist)))
        self.store.discard(principal_id)  # 面談の状態を破棄する(画面のフォームの状態を消すのは、画面の側)
        return {"status": "submitted"}
