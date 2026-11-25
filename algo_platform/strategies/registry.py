"""Strategy template registry.

To add a strategy: create a module in strategies/, subclass Strategy, and decorate the class
with @register (or add it to BUILTIN below). It then appears in the UI automatically.
"""
from __future__ import annotations

from typing import Any

from strategies.base import Strategy, StrategySettings

TEMPLATES: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if not getattr(cls, "template_id", None):
        raise ValueError(f"{cls.__name__} needs a template_id")
    if cls.template_id in TEMPLATES and TEMPLATES[cls.template_id] is not cls:
        raise ValueError(f"duplicate strategy template id {cls.template_id!r}")
    TEMPLATES[cls.template_id] = cls
    return cls


_builtin_loaded = False


def _load_builtin() -> None:
    global _builtin_loaded
    if not _builtin_loaded:
        _builtin_loaded = True
        from strategies import (breakout_tp, ema_rsi_breakout, nifty_early_breakout,  # noqa: F401
                                nifty_ref_breakout, nifty_two_entry, smc_playbook,  # (register themselves)
                                chandelier_exit, lorentzian_ml, dhurandhar)


def get_template(template_id: str) -> type[Strategy]:
    _load_builtin()
    try:
        return TEMPLATES[template_id]
    except KeyError as exc:
        raise KeyError(f"unknown strategy template {template_id!r}") from exc


def all_templates() -> dict[str, type[Strategy]]:
    _load_builtin()
    return dict(TEMPLATES)


def parse_settings(template_id: str, params: dict[str, Any] | None) -> StrategySettings:
    return get_template(template_id).settings_model.model_validate(params or {})


def default_settings(template_id: str) -> dict[str, Any]:
    return get_template(template_id).settings_model().model_dump(mode="json")
