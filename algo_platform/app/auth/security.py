"""Password hashing (argon2id), TOTP 2FA, server-side sessions, CSRF tokens and login throttling."""
from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta

import pyotp
import qrcode
import qrcode.image.svg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, select

from core.clock import now_ist
from storage.crypto import get_crypto
from storage.db import Database
from storage.models import User, UserSession

_ph = PasswordHasher()  # argon2id with library defaults (t=3, m=64MiB, p=4)
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LEN = 10


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _ph.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problems(password: str, username: str = "") -> list[str]:
    out = []
    if len(password) < MIN_PASSWORD_LEN:
        out.append(f"must be at least {MIN_PASSWORD_LEN} characters")
    if username and username.lower() in password.lower():
        out.append("must not contain the username")
    if password.isdigit() or password.isalpha():
        out.append("must mix letters with digits or symbols")
    return out


# ------------------------------------------------------------------ TOTP
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Algo Platform")


def totp_qr_svg(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def verify_totp(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def user_totp_secret(user: User) -> str | None:
    if not user.totp_secret_enc:
        return None
    return get_crypto().decrypt(user.totp_secret_enc)


# ------------------------------------------------------------------ sessions
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class SessionInfo:
    session_id: str
    user_id: int
    username: str
    role: str
    csrf_token: str
    pending_2fa: bool
    must_change_password: bool


class SessionStore:
    def __init__(self, db: Database, idle_minutes: int, max_hours: int) -> None:
        self.db = db
        self.idle = timedelta(minutes=idle_minutes)
        self.max_age = timedelta(hours=max_hours)

    def create(self, user: User, ip: str | None, ua: str | None, pending_2fa: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        now = now_ist()
        with self.db.session() as s:
            s.add(UserSession(id=_hash_token(token), user_id=user.id, csrf_token=secrets.token_urlsafe(32),
                              created_at=now, last_seen_at=now, expires_at=now + self.max_age, ip=ip,
                              user_agent=(ua or "")[:250], pending_2fa=pending_2fa))
        return token

    def get(self, token: str | None, touch: bool = True) -> SessionInfo | None:
        if not token:
            return None
        sid = _hash_token(token)
        now = now_ist()
        with self.db.session() as s:
            row = s.get(UserSession, sid)
            if row is None:
                return None
            if now > row.expires_at or now - row.last_seen_at > self.idle:
                s.delete(row)
                return None
            user = s.get(User, row.user_id)
            if user is None or not user.is_active:
                s.delete(row)
                return None
            if touch:
                row.last_seen_at = now
            return SessionInfo(sid, user.id, user.username, user.role, row.csrf_token, row.pending_2fa,
                               user.must_change_password)

    def complete_2fa(self, token: str) -> None:
        with self.db.session() as s:
            row = s.get(UserSession, _hash_token(token))
            if row is not None:
                row.pending_2fa = False

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self.db.session() as s:
            row = s.get(UserSession, _hash_token(token))
            if row is not None:
                s.delete(row)

    def destroy_user_sessions(self, user_id: int, except_sid: str | None = None) -> None:
        with self.db.session() as s:
            q = delete(UserSession).where(UserSession.user_id == user_id)
            if except_sid:
                q = q.where(UserSession.id != except_sid)
            s.execute(q)

    def purge_expired(self) -> None:
        now = now_ist()
        with self.db.session() as s:
            s.execute(delete(UserSession).where((UserSession.expires_at < now)
                                                | (UserSession.last_seen_at < now - self.idle)))


def csrf_equal(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


# ------------------------------------------------------------------ throttling / lockout
class LoginThrottle:
    """Per-IP sliding window limit on login attempts (in memory)."""

    def __init__(self, max_attempts: int = 10, window_sec: int = 300) -> None:
        self.max = max_attempts
        self.window = window_sec
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        q = self._hits[ip]
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) < self.max

    def hit(self, ip: str) -> None:
        self._hits[ip].append(time.monotonic())

    def reset(self, ip: str) -> None:
        self._hits.pop(ip, None)


def is_locked(user: User) -> bool:
    return user.locked_until is not None and user.locked_until > now_ist()


def register_failure(db: Database, user_id: int) -> bool:
    """Returns True if the account is now locked."""
    with db.session() as s:
        u = s.get(User, user_id)
        if u is None:
            return False
        u.failed_attempts = (u.failed_attempts or 0) + 1
        if u.failed_attempts >= MAX_FAILED_ATTEMPTS:
            u.locked_until = now_ist() + timedelta(minutes=LOCKOUT_MINUTES)
            u.failed_attempts = 0
            return True
        return False


def register_success(db: Database, user_id: int) -> None:
    with db.session() as s:
        u = s.get(User, user_id)
        if u is not None:
            u.failed_attempts = 0
            u.locked_until = None
            u.last_login_at = now_ist()


def user_count(db: Database) -> int:
    with db.session() as s:
        return len(list(s.scalars(select(User.id))))


def find_user(db: Database, username: str) -> User | None:
    with db.session() as s:
        u = s.scalar(select(User).where(User.username == username.strip().lower()))
        if u is not None:
            s.expunge(u)
        return u


def dummy_verify() -> None:
    """Spend the same time as a real verification so unknown usernames aren't distinguishable."""
    verify_password(_DUMMY_HASH, "not-the-password")


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def lockout_remaining(user: User) -> int:
    if not user.locked_until:
        return 0
    return max(0, int((user.locked_until - now_ist()).total_seconds() // 60) + 1)


def utcnow() -> datetime:
    return now_ist()
