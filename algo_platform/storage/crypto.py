"""Credential encryption at rest (Fernet / AES-128-CBC + HMAC-SHA256).

The master key comes from the ALGO_MASTER_KEY environment variable and is never written to the
database. Generate one with:  python run.py genkey
"""
from __future__ import annotations

import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from core.masking import mask_value


class MasterKeyError(RuntimeError):
    pass


class Crypto:
    def __init__(self, key: str | bytes | None = None) -> None:
        key = key if key is not None else os.environ.get("ALGO_MASTER_KEY", "")
        if not key:
            raise MasterKeyError("ALGO_MASTER_KEY is not set. Generate one with `python run.py genkey` "
                                 "and put it in your environment or .env file.")
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise MasterKeyError("ALGO_MASTER_KEY is not a valid Fernet key (32 url-safe base64 bytes)") from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise MasterKeyError("could not decrypt stored secret: wrong ALGO_MASTER_KEY or corrupted data") from exc

    def encrypt_json(self, data: dict[str, Any]) -> str:
        return self.encrypt(json.dumps(data, separators=(",", ":")))

    def decrypt_json(self, token: str) -> dict[str, Any]:
        if not token:
            return {}
        return json.loads(self.decrypt(token))


def masked_credentials(data: dict[str, Any], secret_fields: set[str]) -> dict[str, str]:
    """For display: secrets masked, other fields as-is."""
    out = {}
    for k, v in data.items():
        out[k] = mask_value(v) if k in secret_fields else ("" if v is None else str(v))
    return out


_crypto: Crypto | None = None


def get_crypto() -> Crypto:
    global _crypto
    if _crypto is None:
        _crypto = Crypto()
    return _crypto


def set_crypto(c: Crypto | None) -> None:
    global _crypto
    _crypto = c
