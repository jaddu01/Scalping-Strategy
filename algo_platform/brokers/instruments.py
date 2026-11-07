"""In-memory instrument master index shared by adapters, plus price rounding helpers."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

from brokers.base import Exchange, Instrument, InstrumentKey, InstrumentType


class InstrumentNotFound(LookupError):
    pass


class InstrumentStore:
    def __init__(self) -> None:
        self._by_token: dict[tuple[str, str], Instrument] = {}
        self._options: dict[tuple[str, str], dict[tuple[date, float, str], Instrument]] = defaultdict(dict)
        self._futures: dict[tuple[str, str], dict[date, Instrument]] = defaultdict(dict)
        self._underlying: dict[tuple[str, str, str], Instrument] = {}  # (exch, name, type) -> index/eq

    def __len__(self) -> int:
        return len(self._by_token)

    def clear(self) -> None:
        self._by_token.clear()
        self._options.clear()
        self._futures.clear()
        self._underlying.clear()

    def add(self, inst: Instrument) -> None:
        self._by_token[inst.key] = inst
        k = (inst.exchange.value, inst.underlying.upper())
        if inst.instrument_type in (InstrumentType.CE, InstrumentType.PE) and inst.expiry and inst.strike is not None:
            self._options[k][(inst.expiry, float(inst.strike), inst.instrument_type.value)] = inst
        elif inst.instrument_type is InstrumentType.FUT and inst.expiry:
            self._futures[k][inst.expiry] = inst
        else:
            self._underlying[(inst.exchange.value, inst.underlying.upper(), inst.instrument_type.value)] = inst

    def by_token(self, exchange: Exchange, token: str) -> Instrument | None:
        return self._by_token.get((exchange.value, str(token)))

    def all(self) -> list[Instrument]:
        return list(self._by_token.values())

    def option_expiries(self, underlying: str, exchange: Exchange = Exchange.NFO) -> list[date]:
        opts = self._options.get((exchange.value, underlying.upper()), {})
        return sorted({k[0] for k in opts})

    def strikes(self, underlying: str, expiry: date, exchange: Exchange = Exchange.NFO) -> list[float]:
        opts = self._options.get((exchange.value, underlying.upper()), {})
        return sorted({k[1] for k in opts if k[0] == expiry})

    def resolve(self, key: InstrumentKey) -> Instrument:
        exch = key.exchange.value
        name = key.underlying.upper()
        if key.instrument_type in (InstrumentType.CE, InstrumentType.PE):
            if key.expiry is None or key.strike is None:
                raise InstrumentNotFound(f"option key needs expiry and strike: {key}")
            inst = self._options.get((exch, name), {}).get((key.expiry, float(key.strike), key.instrument_type.value))
            if inst is None:
                raise InstrumentNotFound(f"no option {key.label()} on {exch}")
            return inst
        if key.instrument_type is InstrumentType.FUT:
            futs = self._futures.get((exch, name), {})
            if key.expiry:
                inst = futs.get(key.expiry)
            else:
                inst = futs[min(futs)] if futs else None
            if inst is None:
                raise InstrumentNotFound(f"no future {name} {key.expiry} on {exch}")
            return inst
        inst = self._underlying.get((exch, name, key.instrument_type.value))
        if inst is None:
            raise InstrumentNotFound(f"no {key.instrument_type.value} {name} on {exch}")
        return inst


def round_to_tick(price: float, tick: float = 0.05, mode: str = "nearest") -> float:
    """Round a price to the instrument tick size. mode: nearest | up | down."""
    if tick <= 0:
        return round(price, 2)
    q = price / tick
    if mode == "up":
        n = math.ceil(q - 1e-9)
    elif mode == "down":
        n = math.floor(q + 1e-9)
    else:
        n = math.floor(q + 0.5)
    return round(n * tick, 2)
