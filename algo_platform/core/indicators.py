"""Streaming indicators matching TradingView Pine's ta.ema, ta.rma, ta.rsi and ta.atr.

Each indicator takes one closed candle at a time and returns None until it has enough data.
EMA and RMA are seeded with the simple average of the first `length` values (as Pine's ta.rma; after
the usual warm-up of a few hundred candles the seed no longer matters for ta.ema either).
"""
from __future__ import annotations


class EMA:
    def __init__(self, length: int) -> None:
        self.length = length
        self.alpha = 2.0 / (length + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.length:
                self.value = sum(self._seed) / self.length
            return self.value
        self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class RMA:
    """Wilder's moving average (ta.rma): alpha = 1 / length."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.length:
                self.value = sum(self._seed) / self.length
            return self.value
        self.value = (x + (self.length - 1) * self.value) / self.length
        return self.value


class RSI:
    """ta.rsi(close, length): RMA of gains and losses."""

    def __init__(self, length: int) -> None:
        self.up, self.down = RMA(length), RMA(length)
        self.prev: float | None = None
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self.prev is None:
            self.prev = close
            return None
        change = close - self.prev
        self.prev = close
        up, down = self.up.update(max(change, 0.0)), self.down.update(max(-change, 0.0))
        if up is None or down is None:
            return None
        self.value = 100.0 if down == 0 else 0.0 if up == 0 else 100.0 - 100.0 / (1.0 + up / down)
        return self.value

    def peek(self, close: float) -> float | None:
        """RSI if the forming candle closed at `close` now (TradingView's real-time value); state unchanged."""
        if self.prev is None or self.up.value is None or self.down.value is None:
            return None
        change = close - self.prev
        n = self.up.length
        up = (max(change, 0.0) + (n - 1) * self.up.value) / n
        down = (max(-change, 0.0) + (n - 1) * self.down.value) / n
        return 100.0 if down == 0 else 0.0 if up == 0 else 100.0 - 100.0 / (1.0 + up / down)


class ATR:
    """ta.atr(length): RMA of the true range (first candle: high − low)."""

    def __init__(self, length: int) -> None:
        self.rma = RMA(length)
        self.prev_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = high - low if self.prev_close is None else max(high - low, abs(high - self.prev_close),
                                                           abs(low - self.prev_close))
        self.prev_close = close
        self.value = self.rma.update(tr)
        return self.value
