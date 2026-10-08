"""面談の途中の状態(design.md §5 の 9・§1.2・§7)。サーバのメモリに、依頼者 ID ごとに持つ。

- 生の値(年収の数字・自由コメントや辞めた理由から取り出した条件など)は、Firestore にも金庫にも書かない。この module は、メモリの上に
  持つだけ(永続化しない。web が落ちれば消えるので、面談はやり直しになる)。
- 寿命: 次のどれかで消える。
  - 送信(web.interview.service.submit)か破棄(discard)。
  - 本人の「データを消す」と、30 日の自動削除(依頼者の見回り web.principal_sweeper)。どちらも web.deletion が、流れの最初に discard する
    (本人のボタンは、面談を送る前で利用記録がない依頼者でも、途中の状態はメモリにあるので消す)。
  - 放置: 最後に書き込んでからの秒数(state_idle_ttl_seconds)で読めなくなり(get は None)、メモリからは、新しい面談を作るとき(create)と、
    交渉の見回りが定期に呼ぶ evict_idle で消す(台帳 C-66・L19-6: 新しい面談が始まらなければ、放置された面談の生の年収や取り出した発言が、
    再起動までメモリに残ってしまうため)。アイドルの時計は、書き込み(get の touch=True。開始・プロフィール・回答・確認など)でだけ進め直し、
    読み取り(GET)では延ばさない(台帳 C-69・X-87: 読み取りで延びると、持ち主が 1 時間に 1 回読むだけで、状態を持ち続けられる)。
  - 絶対の寿命: 作ってから max_lifetime_seconds(3 時間)で、アイドルかどうか・読み書きの有無によらず、読めなくなり(get は None)、evict_idle・create で消す(台帳 C-69・X-87)。
  同時に持てる面談の数にも上限を置く(開始ページを何度も開くだけの訪問者で、メモリを埋められないように。面談の開始の口には、入口の枠 interview_begin も掛ける)。
  全体の上限(max_states)とは別に、状態を作った送信元(web.client_ip.client_key。IPv6 は /64 単位)ごとの上限(max_per_client。暫定 3)も置く: 1 つの送信元が、
  全体の枠を占有しないように(台帳 C-69・X-87)。新しく作るとき(create)にだけ数える。数えるのは、いま読める状態(寿命を過ぎたものは数えない)。
- 持たないもの: 3 問の回答・自由コメント・辞めた理由の原文(LLM に送ったあとは、どこにも持たない。取り出した発言だけを持つ)、
  プロフィールの正確な値(経験年数・都道府県は、帯に変換した時点で捨てる)。
- revision: 確認の対象(二択の回答・発言・項目の有無・外した軸・年収)を変えるたびに 1 進める。確認と「最悪ここまで」の承認は、
  そのときの revision を覚える。あとで内容を変えれば revision が進み、確認し直しが要る(送信は、現在の revision で確認・承認済みのときだけ)。
"""

import datetime as dt
import math
from dataclasses import dataclass, field

from negotiation_core import CandidateAttributeBands

from vault.clock import Clock

from web.client_ip import UNKNOWN_CLIENT
from web.interview.anchors import ChoiceAnswer, StatementRecord
from web.interview.salary import NormalizedSalary, SalaryBasis


class InterviewStoreFull(Exception):
    """同時に持てる面談の数の上限に達している。"""


class InterviewClientLimitReached(Exception):
    """この送信元が、すでに同時に持てる面談の数(max_per_client)だけ持っている(台帳 C-69・X-87)。

    limit はその上限の数、retry_after_seconds は、この送信元の状態のどれかが(使われないまま)寿命を過ぎて、席が空くまでの最短の秒数(1 以上)。
    """

    def __init__(self, *, limit: int, retry_after_seconds: int) -> None:
        super().__init__("too many interviews for this client")
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class SalaryProposal:
    """3 問の回答から読み取った年収の定義と、その換算の結果(本人が確かめる前のもの)。"""

    basis: SalaryBasis
    normalized: NormalizedSalary


@dataclass
class InterviewState:
    """1 人の依頼者の、面談の途中の状態。

    created_at は作った時刻(絶対の寿命の起点)、touched_at は最後に書き込んだ時刻(アイドルの寿命の起点)。client は、作った送信元のキー
    (web.client_ip.client_key。同時数の上限の数え方に使う。IP なので repr には出さない)。
    """

    created_at: dt.datetime
    touched_at: dt.datetime
    client: str = field(default=UNKNOWN_CLIENT, repr=False)
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
    """依頼者 ID → 面談の状態。時刻は注入できる時計から取る。

    idle_ttl_seconds は最後に書き込んでからの寿命、max_lifetime_seconds は作ってからの絶対の寿命(どちらか早い方で読めなくなる。台帳 C-69・X-87)、
    max_states は全体の同時の数の上限、max_per_client は状態を作った送信元ごとの同時の数の上限。
    """

    def __init__(
        self, clock: Clock, *, idle_ttl_seconds: float, max_lifetime_seconds: float, max_states: int, max_per_client: int
    ) -> None:
        self._clock = clock
        self._idle_ttl = dt.timedelta(seconds=idle_ttl_seconds)
        self._max_lifetime = dt.timedelta(seconds=max_lifetime_seconds)
        self._max_states = max_states
        self._max_per_client = max_per_client
        self._states: dict[str, InterviewState] = {}

    def __len__(self) -> int:
        return len(self._states)

    def _expires_at(self, state: InterviewState) -> dt.datetime:
        """この状態が読めなくなる時刻: 最後に書き込んでからのアイドルの寿命と、作ってからの絶対の寿命の、早い方。"""
        return min(state.touched_at + self._idle_ttl, state.created_at + self._max_lifetime)

    def _expired(self, state: InterviewState, now: dt.datetime) -> bool:
        return now >= self._expires_at(state)

    def evict_idle(self, now: dt.datetime) -> int:
        """now の時点で、寿命を過ぎた面談(最後に書き込んでからのアイドルの寿命か、作ってからの絶対の寿命)を、メモリから消す
        (交渉の見回りが、60 秒ごとに呼ぶ。台帳 C-66・L19-6・C-69)。消した数を返す。
        """
        stale = [pid for pid, state in self._states.items() if self._expired(state, now)]
        for pid in stale:
            del self._states[pid]
        return len(stale)

    def purge_expired(self) -> int:
        """寿命を過ぎた面談を、メモリから消す(create が呼ぶ)。消した数を返す。"""
        return self.evict_idle(self._clock.now())

    def get(self, principal_id: str, *, touch: bool = False) -> InterviewState | None:
        """面談の状態(なければ None)。寿命を過ぎていれば消して None。

        touch=True は書き込み(開始・プロフィール・回答・確認など): 最後に書き込んだ時刻を更新して、アイドルの寿命を延ばす。既定は読み取りで、延ばさない
        (台帳 C-69・X-87: 読み取りで延びると、持ち主が 1 時間に 1 回読むだけで、状態を持ち続けられる)。作ってからの絶対の寿命は、どちらでも延びない。
        """
        state = self._states.get(principal_id)
        if state is None:
            return None
        now = self._clock.now()
        if self._expired(state, now):
            del self._states[principal_id]
            return None
        if touch:
            state.touched_at = now
        return state

    def create(self, principal_id: str, client: str = UNKNOWN_CLIENT) -> InterviewState:
        """新しい面談の状態を作る(すでにあれば、置き換える)。client は、作る送信元のキー(web.client_ip.client_key。分からなければ共有の unknown)。

        置き換える状態は、数えない(同時に max_per_client 件持っている送信元が、その 1 件をやり直しても、断られない)。断るのは、置き換えるもの以外で、
        この送信元が max_per_client 件持っているとき(InterviewClientLimitReached。台帳 C-69・X-87)と、全体が max_states 件に達しているとき
        (InterviewStoreFull)。送信元の方を先に見る。断ったときは、いまの状態に触れない(置き換えるはずだった状態も残る)。
        """
        self.purge_expired()
        held = [state for pid, state in self._states.items() if pid != principal_id and state.client == client]
        if len(held) >= self._max_per_client:
            soonest = min(self._expires_at(state) for state in held)
            raise InterviewClientLimitReached(
                limit=self._max_per_client,
                retry_after_seconds=max(1, math.ceil((soonest - self._clock.now()).total_seconds())),
            )
        others = len(self._states) - 1 if principal_id in self._states else len(self._states)  # 置き換える 1 件は、数えない
        if others >= self._max_states:
            raise InterviewStoreFull
        now = self._clock.now()
        state = self._states[principal_id] = InterviewState(created_at=now, touched_at=now, client=client)
        return state

    def discard(self, principal_id: str) -> bool:
        """面談の状態を消す(送信のあと・破棄のとき)。あったら True。"""
        return self._states.pop(principal_id, None) is not None
