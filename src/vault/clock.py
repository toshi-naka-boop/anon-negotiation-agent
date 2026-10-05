"""金庫に注入できる時計(design.md 完了条件の前提: テストは sleep せず時計を進めて確かめる)。

金庫のコードは、現在時刻が要る場所ではすべてこのモジュールの Clock を受け取り、
`datetime.now()` を直接呼ばない。本番は SystemClock、テストは FixedClock を使う。
"""

import datetime as dt
from typing import Protocol


class Clock(Protocol):
    """現在時刻を返すだけの最小のプロトコル。"""

    def now(self) -> dt.datetime: ...


class SystemClock:
    """本番用の時計(現在の UTC 時刻を返す)。"""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)


class FixedClock:
    """テスト用の時計。sleep の代わりに advance()/set() で進める。"""

    def __init__(self, initial: dt.datetime | None = None) -> None:
        self._now = initial if initial is not None else dt.datetime.now(dt.timezone.utc)

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, delta: dt.timedelta) -> None:
        self._now = self._now + delta

    def set(self, value: dt.datetime) -> None:
        self._now = value
