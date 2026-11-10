"""Generate HTML form fields from a Pydantic model and parse submitted forms back into it.

Used for strategy settings (each template's settings_model), broker credentials (each adapter's
credentials_model), risk limits and Global Settings — so adding a field to a model is all it
takes to make it editable in the UI, with validation errors shown inline.
"""
from __future__ import annotations

import types
import typing
from dataclasses import dataclass, field
from datetime import time
from enum import Enum
from typing import Any, Literal, get_args, get_origin

import annotated_types as at
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from core.clock import fmt_hm


@dataclass
class FormField:
    name: str  # dotted path, e.g. "risk.max_daily_loss"
    label: str
    kind: str  # text|password|number|checkbox|select|multiselect|time|list|textarea|group
    value: Any = None
    description: str = ""
    required: bool = False
    options: list[tuple[str, str]] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    step: str = "any"
    secret: bool = False
    has_saved_secret: bool = False
    error: str | None = None
    children: list["FormField"] = field(default_factory=list)


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    origin = get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return tp, False


def _constraints(fi: FieldInfo) -> tuple[float | None, float | None]:
    lo = hi = None
    for m in fi.metadata:
        if isinstance(m, at.Ge):
            lo = m.ge
        elif isinstance(m, at.Gt):
            lo = m.gt
        elif isinstance(m, at.Le):
            hi = m.le
        elif isinstance(m, at.Lt):
            hi = m.lt
    return lo, hi


def _fmt(v: Any) -> Any:
    if isinstance(v, time):
        return fmt_hm(v)
    if isinstance(v, Enum):
        return v.value
    return v


def build_fields(model: type[BaseModel], values: dict[str, Any] | BaseModel | None = None,
                 errors: dict[str, str] | None = None, prefix: str = "",
                 choices: dict[str, list[tuple[str, str]]] | None = None,
                 saved_secrets: set[str] | None = None) -> list[FormField]:
    if isinstance(values, BaseModel):
        values = values.model_dump()
    values = values or {}
    errors = errors or {}
    choices = choices or {}
    saved_secrets = saved_secrets or set()
    out: list[FormField] = []
    # a model can list the fields to show first (e.g. a strategy's schedule) via FORM_FIRST
    first = [n for n in getattr(model, "FORM_FIRST", ()) if n in model.model_fields]
    ordered = first + [n for n in model.model_fields if n not in first]
    for name in ordered:
        fi = model.model_fields[name]
        path = f"{prefix}{name}"
        tp, optional = _unwrap_optional(fi.annotation)
        label = fi.title or name.replace("_", " ").capitalize()
        desc = fi.description or ""
        extra = fi.json_schema_extra if isinstance(fi.json_schema_extra, dict) else {}
        if name in values:
            val = values[name]
        elif fi.default_factory is not None:
            val = fi.default_factory()  # type: ignore[call-arg]
        else:
            val = None if fi.is_required() else fi.default
        f = FormField(name=path, label=label, kind="text", description=desc, error=errors.get(path),
                      required=fi.is_required() and not optional)
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            f.kind = "group"
            f.children = build_fields(tp, val if isinstance(val, (dict, BaseModel)) else None, errors, f"{path}.",
                                      choices, saved_secrets)
            out.append(f)
            continue
        if path in choices:
            f.kind = "select"
            f.options = ([("", "— none —")] if optional else []) + choices[path]
            f.value = "" if val is None else str(_fmt(val))
        elif tp is bool:
            f.kind, f.value = "checkbox", bool(val)
        elif tp in (int, float):
            f.kind = "number"
            f.min, f.max = _constraints(fi)
            f.step = "1" if tp is int else "any"
            f.value = "" if val is None else val
        elif tp is time:
            f.kind, f.value = "time", _fmt(val) if val else ""
        elif isinstance(tp, type) and issubclass(tp, Enum):
            f.kind = "select"
            f.options = [(m.value, m.value) for m in tp]
            f.value = _fmt(val)
        elif get_origin(tp) is Literal:
            f.kind = "select"
            f.options = [(str(a), str(a)) for a in get_args(tp)]
            f.value = "" if val is None else str(val)
        elif get_origin(tp) is list:
            (inner,) = get_args(tp) or (str,)
            if get_origin(inner) is Literal:
                f.kind = "multiselect"
                f.options = [(str(a), str(a)) for a in get_args(inner)]
                f.value = [str(_fmt(v)) for v in (val or [])]
            else:
                f.kind = "list"
                f.value = ", ".join(str(_fmt(v)) for v in (val or []))
        else:
            secret = bool(extra.get("secret"))
            f.secret = secret
            f.kind = "password" if secret else ("textarea" if extra.get("multiline") else "text")
            f.has_saved_secret = secret and path in saved_secrets
            f.value = "" if secret else ("" if val is None else str(val))
            if secret and f.has_saved_secret:
                f.required = False
        out.append(f)
    return out


def parse_form(model: type[BaseModel], form: Any, prefix: str = "", keep_secrets: dict[str, Any] | None = None
               ) -> dict[str, Any]:
    """Turn submitted form data into a dict ready for model.model_validate().

    Blank secret fields are filled from keep_secrets (so saved secrets are not overwritten)."""
    out: dict[str, Any] = {}
    keep_secrets = keep_secrets or {}
    for name, fi in model.model_fields.items():
        path = f"{prefix}{name}"
        tp, optional = _unwrap_optional(fi.annotation)
        extra = fi.json_schema_extra if isinstance(fi.json_schema_extra, dict) else {}
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            out[name] = parse_form(tp, form, f"{path}.", keep_secrets.get(name) if isinstance(keep_secrets.get(name), dict) else {})
            continue
        if tp is bool:
            out[name] = form.get(path) in ("on", "true", "1", "yes")
            continue
        if get_origin(tp) is list:
            (inner,) = get_args(tp) or (str,)
            if get_origin(inner) is Literal:
                out[name] = list(form.getlist(path)) if hasattr(form, "getlist") else form.get(path, [])
            else:
                raw = (form.get(path) or "").strip()
                out[name] = [p.strip() for p in raw.replace(";", ",").replace("\n", ",").split(",") if p.strip()]
            continue
        raw = form.get(path)
        if raw is None:
            continue
        raw = str(raw).strip()
        if extra.get("secret") and raw == "" and name in keep_secrets:
            out[name] = keep_secrets[name]
            continue
        if raw == "":
            if optional:
                out[name] = None
            elif tp is str:
                out[name] = ""
            continue
        out[name] = raw
    return out


def errors_by_field(exc: ValidationError, prefix: str = "") -> tuple[dict[str, str], list[str]]:
    """ValidationError -> ({dotted field path: message}, [form-level messages])."""
    fields: dict[str, str] = {}
    general: list[str] = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if not isinstance(p, int)]
        msg = err.get("msg", "invalid value").removeprefix("Value error, ")
        if loc:
            fields.setdefault(prefix + ".".join(loc), msg)
        else:
            general.append(msg)
    return fields, general
