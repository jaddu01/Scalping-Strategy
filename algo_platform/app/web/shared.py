"""Shared strategies: one set of rules, run by each partner with their own terms.

A run (StrategyInstance) keeps its full, ready-to-use params (so the engine, backtests and exports work
unchanged): the shared strategy's rules with the run's own PERSONAL fields on top. When the rules change,
every run's params are rebuilt and running ones pick the change up from their next trade slot.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

# what each partner sets for themselves; everything else is the shared rules
PERSONAL_PARAMS = ("lots", "tp1_lots", "tp2_lots")


def personal_keys(model: type[BaseModel]) -> list[str]:
    return [k for k in PERSONAL_PARAMS if k in model.model_fields]


def merged(model: type[BaseModel], rules: dict[str, Any], own: dict[str, Any]) -> dict[str, Any]:
    """Raw (unvalidated) params of a run: the rules + the run's personal values."""
    out = {k: v for k, v in (rules or {}).items() if k not in PERSONAL_PARAMS}
    for k in personal_keys(model):
        if k in (own or {}):
            out[k] = own[k]
        elif k in (rules or {}):
            out[k] = rules[k]
    return out


def split_fields(fields: list[Any], personal: bool) -> list[Any]:
    """The personal (lots) part or the rules part of a strategy's form fields."""
    return [f for f in fields if (f.name.split(".", 1)[-1] in PERSONAL_PARAMS) == personal]


def check_runs(model: type[BaseModel], rules: dict[str, Any],
               runs: list[tuple[str, dict[str, Any]]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate new rules against every run's own lots. Returns ({run name: params}, problems)."""
    ok: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for name, own in runs:
        try:
            ok[name] = model.model_validate(merged(model, rules, own)).model_dump(mode="json")
        except ValidationError as exc:
            msg = "; ".join(e.get("msg", "invalid").removeprefix("Value error, ") for e in exc.errors())
            problems.append(f"{name}: {msg}")
    return ok, problems
