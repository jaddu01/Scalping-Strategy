"""Masking of secrets in anything that gets logged, stored as an event, or shown in the UI."""
from __future__ import annotations

import re
from typing import Any

_SECRET_SUBSTRINGS = ("password", "passwd", "totp", "secret", "jwt", "authorization", "apikey",
                      "privatekey", "accesstoken", "refreshtoken", "feedtoken", "authtoken", "bottoken",
                      "sessiontoken", "masterkey")
_SECRET_EXACT = {"pin", "mpin", "pwd", "key", "otp", "csrf", "csrftoken"}
# Identifiers that end in "token" but are not secret (instrument tokens).
_SAFE_TOKENS = {"token", "symboltoken", "instrumenttoken", "exchangetoken", "tokens", "spottoken",
                "underlyingtoken", "optiontoken"}

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=]+", re.IGNORECASE)


def mask_value(value: Any, keep: int = 2) -> str:
    if value is None or value == "":
        return ""
    s = str(value)
    if len(s) <= keep * 2:
        return "•" * len(s)
    return s[:keep] + "•" * min(8, len(s) - keep * 2) + s[-keep:]


def is_secret_key(key: str) -> bool:
    k = re.sub(r"[^a-z0-9]", "", key.lower())
    if k in _SAFE_TOKENS:
        return False
    if k in _SECRET_EXACT or k.endswith("pin"):
        return True
    if k.endswith("token"):
        return True
    return any(sub in k for sub in _SECRET_SUBSTRINGS)


def mask_text(text: str) -> str:
    text = _JWT_RE.sub("eyJ•••masked•••", text)
    return _BEARER_RE.sub(r"\1•••masked•••", text)


def mask_secrets(obj: Any, _depth: int = 0) -> Any:
    """Return a copy of obj with secret-looking fields masked (recursive)."""
    if _depth > 8:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and is_secret_key(k) and not isinstance(v, (dict, list)):
                out[k] = "•••" if v not in (None, "") else v
            else:
                out[k] = mask_secrets(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [mask_secrets(v, _depth + 1) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj
