"""Recent-bar history with Pine-style indexing: `h.high(2)` is Pine's `high[2]`.

Asking for a bar older than what has been pushed returns `None` (Pine's `na`). In Pine
any comparison with `na` is false; in Python `None` cannot be compared at all, so a
caller tests `h.has(n)` (or the value for `None`) before using it.

Asking beyond `maxlen` is a configuration error, not `na`: it raises, because a silent
`None` there would quietly switch a setup off forever (Pine's max_bars_back error).
"""

from collections import deque

from propbot.core.bars import Bar


class HistoryError(ValueError):
    """A bad history size, offset or bar order. The message is user-facing (Turkish)."""


class BarHistory:
    """A ring of the most recent `maxlen` bars; offset 0 is the latest pushed bar."""

    __slots__ = ("_bars", "_maxlen")

    def __init__(self, maxlen: int) -> None:
        if isinstance(maxlen, bool) or not isinstance(maxlen, int) or maxlen < 1:
            raise HistoryError(f"bar geçmişi boyu 1 veya daha büyük olmalı: {maxlen!r}")
        self._maxlen = maxlen
        self._bars: deque[Bar] = deque(maxlen=maxlen)

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def __len__(self) -> int:
        return len(self._bars)

    def push(self, bar: Bar) -> None:
        if self._bars and bar.ts_open_ms <= self._bars[-1].ts_open_ms:
            raise HistoryError(
                "barlar açılış anına göre kesin artan sırada olmalı "
                f"({self._bars[-1].ts_open_ms} → {bar.ts_open_ms})"
            )
        self._bars.append(bar)

    def has(self, n: int) -> bool:
        """True when `bar(n)` exists, i.e. Pine's `x[n]` is not na."""
        return self._check_offset(n) < len(self._bars)

    def bar(self, n: int = 0) -> Bar | None:
        if not self.has(n):
            return None
        return self._bars[-1 - n]

    def time(self, n: int = 0) -> int | None:
        b = self.bar(n)
        return None if b is None else b.ts_open_ms

    def open(self, n: int = 0) -> float | None:
        b = self.bar(n)
        return None if b is None else b.open

    def high(self, n: int = 0) -> float | None:
        b = self.bar(n)
        return None if b is None else b.high

    def low(self, n: int = 0) -> float | None:
        b = self.bar(n)
        return None if b is None else b.low

    def close(self, n: int = 0) -> float | None:
        b = self.bar(n)
        return None if b is None else b.close

    def volume(self, n: int = 0) -> float | None:
        b = self.bar(n)
        return None if b is None else b.volume

    def _check_offset(self, n: int) -> int:
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise HistoryError(f"geçmiş ofseti negatif olmayan bir tam sayı olmalı: {n!r}")
        if n >= self._maxlen:
            raise HistoryError(
                f"geçmiş ofseti {n}, tutulan bar sayısını ({self._maxlen}) aşıyor; "
                "geçmiş boyunu büyütün"
            )
        return n
