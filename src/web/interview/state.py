"""面談の途中の状態(design.md §5 の 9・§1.2・§7)。サーバのメモリに、依頼者 ID ごとに持つ。

- 生の値(年収の数字・自由コメントや辞めた理由から取り出した条件など)は、Firestore にも金庫にも書かない。この module は、メモリの上に
  持つだけ(永続化しない。web が落ちれば消えるので、面談はやり直しになる)。
- 寿命: 送信(web.interview.service.submit)か破棄(discard)で消す。放置された面談は、最後に使ってからの秒数(state_idle_ttl_seconds)で
  読めなくなり(get は None)、メモリからは、新しい面談を作るとき(create)と、交渉の見回りが定期に呼ぶ evict_idle で消す(台帳 C-66・L19-6:
  新しい面談が始まらなければ、放置された面談の生の年収や取り出した発言が、再起動までメモリに残ってしまうため)。
  同時に持てる面談の数にも上限を置く(開始ページを何度も開くだけの訪問者で、メモリを埋められないように。面談の開始の口には、入口の枠 interview_begin も掛ける)。
- 持たないもの: 3 問の回答・自由コメント・辞めた理由の原文(LLM に送ったあとは、どこにも持たない。取り出した発言だけを持つ)、
  プロフィールの正確な値(経験年数・都道府県は、帯に変換した時点で捨てる)。
- revision: 確認の対象(二択の回答・発言・項目の有無・外した軸・年収)を変えるたびに 1 進める。確認と「最悪ここまで」の承認は、
  そのときの revision を覚える。あとで内容を変えれば revision が進み、確認し直しが要る(送信は、現在の revision で確認・承認済みのときだけ)。
"""

import datetime as dt
from dataclasses import dataclass, field

from negotiation_core import CandidateAttributeBands

from vault.clock import Clock

from web.interview.anchors import ChoiceAnswer, StatementRecord
from web.interview.salary import NormalizedSalary, SalaryBasis


class InterviewStoreFull(Exception):
    """同時に持てる面談の数の上限に達している。"""


@dataclass(frozen=True)
class SalaryProposal:
    """3 問の回答から読み取った年収の定義と、その換算の結果(本人が確かめる前のもの)。"""

    basis: SalaryBasis
    normalized: NormalizedSalary


@dataclass
class InterviewState:
    """1 人の依頼者の、面談の途中の状態。"""

    created_at: dt.datetime
    touched_at: dt.datetime
    revision: int = 0
    bands: CandidateAttributeBands | None = None
    salary_proposal: SalaryProposal | None = None
    salary: NormalizedSalary | None = None  # 本人が確かめた比較基準年収
    removed_axes: tuple[str, ...] | None = None  # None は、軸を外すかどうかの手順を、まだ終えていない
    answers: dict[tuple[str, str], ChoiceAnswer] = field(default_factory=dict)  # (組の ID, "a" または "b") → 回答
    statements: list[StatementRecord] = field(default_factory=list)
    inactive: set[str] = field(default_factory=set)  # 本人が消した項目のキー
    extractions: int = 0  # 自由コメント・辞めた理由を取り出した回数(発言のキーの番号)
    confirmed_revision: int | None = None
    worst_case_revision: int | None = None
    blocklist: list[str] | None = None  # None は、ブロック先の手順を行っていない(金庫の既存のブロックリストに触れない)

    def bump(self) -> None:
        """確認の対象が変わった。確認と承認は、やり直しになる。"""
        self.revision += 1


class InterviewStateStore:
    """依頼者 ID → 面談の状態。時刻は注入できる時計から取る。"""

    def __init__(self, clock: Clock, *, idle_ttl_seconds: float, max_states: int) -> None:
        self._clock = clock
        self._idle_ttl = dt.timedelta(seconds=idle_ttl_seconds)
        self._max_states = max_states
        self._states: dict[str, InterviewState] = {}

    def __len__(self) -> int:
        return len(self._states)

    def _expired(self, state: InterviewState, now: dt.datetime) -> bool:
        return now - state.touched_at >= self._idle_ttl

    def evict_idle(self, now: dt.datetime) -> int:
        """now の時点で、最後に使ってから寿命を過ぎた面談を、メモリから消す(交渉の見回りが、60 秒ごとに呼ぶ。台帳 C-66・L19-6)。消した数を返す。"""
        stale = [pid for pid, state in self._states.items() if self._expired(state, now)]
        for pid in stale:
            del self._states[pid]
        return len(stale)

    def purge_expired(self) -> int:
        """最後に使ってから寿命を過ぎた面談を、メモリから消す(create が呼ぶ)。消した数を返す。"""
        return self.evict_idle(self._clock.now())

    def get(self, principal_id: str) -> InterviewState | None:
        """面談の状態(なければ None)。寿命を過ぎていれば消して None。使ったので、最後に使った時刻を更新する。"""
        state = self._states.get(principal_id)
        if state is None:
            return None
        now = self._clock.now()
        if self._expired(state, now):
            del self._states[principal_id]
            return None
        state.touched_at = now
        return state

    def create(self, principal_id: str) -> InterviewState:
        """新しい面談の状態を作る(すでにあれば、捨てて作り直す)。上限に達していれば InterviewStoreFull。"""
        self._states.pop(principal_id, None)
        self.purge_expired()
        if len(self._states) >= self._max_states:
            raise InterviewStoreFull
        now = self._clock.now()
        state = self._states[principal_id] = InterviewState(created_at=now, touched_at=now)
        return state

    def discard(self, principal_id: str) -> bool:
        """面談の状態を消す(送信のあと・破棄のとき)。あったら True。"""
        return self._states.pop(principal_id, None) is not None
