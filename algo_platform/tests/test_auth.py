"""Login/auth: first-run setup, password hashing, CSRF, lockout, rate limit, TOTP 2FA, roles, idle timeout."""
from __future__ import annotations

import re
from datetime import timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.auth import security as sec
from app.main import create_app
from core.config import AppConfig
from storage.crypto import Crypto
from storage.models import User, UserSession

PW = "Correct-horse-9"


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'web.db'}",
                    master_key=Crypto.generate_key(), disable_engine=True, session_idle_minutes=30)
    app = create_app(cfg)
    from app.web import routes_auth

    routes_auth._throttle._hits.clear()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c
    routes_auth._throttle._hits.clear()


def setup_admin(c: TestClient, username: str = "admin", password: str = PW) -> None:
    r = c.get("/setup")
    tok = csrf_from(r.text)
    r = c.post("/setup", data={"username": username, "password": password, "password2": password, "csrf_token": tok},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def login(c: TestClient, username: str = "admin", password: str = PW, follow: bool = False):
    r = c.get("/login")
    tok = csrf_from(r.text)
    return c.post("/login", data={"username": username, "password": password, "csrf_token": tok, "next": "/"},
                  follow_redirects=follow)


def test_first_run_redirects_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    setup_admin(client)
    # setup is closed once an admin exists
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_setup_rejects_weak_password(client):
    tok = csrf_from(client.get("/setup").text)
    r = client.post("/setup", data={"username": "admin", "password": "short", "password2": "short", "csrf_token": tok})
    assert r.status_code == 400 and "at least 10" in r.text


def test_password_is_hashed_with_argon2(client):
    setup_admin(client)
    db = client.app.state.db
    with db.session() as s:
        u = s.query(User).one()
        assert u.password_hash.startswith("$argon2id$") and PW not in u.password_hash
        assert u.role == "admin"


def test_login_success_sets_secure_session_cookie(client):
    setup_admin(client)
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "algo_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/", follow_redirects=False).status_code == 200


def test_cookie_secure_flag_when_https(tmp_path):
    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'w.db'}", master_key=Crypto.generate_key(),
                    disable_engine=True)
    with TestClient(create_app(cfg), base_url="https://127.0.0.1") as c:
        setup_admin(c)
        r = login(c)
        assert "Secure" in r.headers["set-cookie"]


def test_wrong_password_and_lockout(client):
    setup_admin(client)
    for _ in range(sec.MAX_FAILED_ATTEMPTS - 1):
        assert login(client, password="wrong-password-1").status_code == 401
    r = login(client, password="wrong-password-1")
    assert r.status_code == 423
    # even the right password is refused while locked
    assert login(client).status_code == 423


def test_login_rate_limit_per_ip(client):
    setup_admin(client)
    from app.web import routes_auth

    routes_auth._throttle.reset("testclient")
    codes = [login(client, username=f"nobody{i}", password="x").status_code for i in range(12)]
    assert 429 in codes
    routes_auth._throttle._hits.clear()


def test_csrf_required_on_login_and_actions(client):
    setup_admin(client)
    r = client.post("/login", data={"username": "admin", "password": PW, "csrf_token": "bogus"},
                    follow_redirects=False)
    assert r.status_code == 303 and "expired" in r.headers["location"]  # refused, user asked to retry
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")
    login(client)
    # state-changing request without the session CSRF token is refused
    r = client.post("/profile/totp/start", data={})
    assert r.status_code == 403
    tok = csrf_from(client.get("/profile").text)
    r = client.post("/profile/totp/start", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303


def test_unauthenticated_access(client):
    setup_admin(client)
    assert client.get("/strategies", follow_redirects=False).headers["location"].startswith("/login")
    assert client.get("/api/events").status_code == 401


def test_totp_two_factor_flow(client):
    setup_admin(client)
    login(client)
    tok = csrf_from(client.get("/profile").text)
    client.post("/profile/totp/start", data={"csrf_token": tok})
    page = client.get("/profile").text
    secret = re.search(r"Manual key: <code>([A-Z2-7]+)</code>", page).group(1)
    client.post("/profile/totp/confirm", data={"csrf_token": tok, "code": pyotp.TOTP(secret).now()})
    with client.app.state.db.session() as s:
        u = s.query(User).one()
        assert u.totp_enabled and u.totp_secret_enc and secret not in u.totp_secret_enc  # stored encrypted
    client.post("/logout", data={"csrf_token": tok})
    r = login(client)
    assert r.headers["location"].startswith("/login/totp")
    # pending-2FA session cannot reach the app
    assert client.get("/", follow_redirects=False).headers["location"] == "/login/totp"
    page = client.get("/login/totp").text
    t2 = csrf_from(page)
    assert client.post("/login/totp", data={"csrf_token": t2, "code": "000000"}).status_code == 401
    r = client.post("/login/totp", data={"csrf_token": t2, "code": pyotp.TOTP(secret).now(), "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_viewer_is_read_only(client):
    setup_admin(client)
    login(client)
    tok = csrf_from(client.get("/users").text)
    client.post("/users/new", data={"csrf_token": tok, "username": "watcher", "password": "Viewer-pass-123",
                                    "role": "viewer"})
    client.post("/logout", data={"csrf_token": tok})
    login(client, "watcher", "Viewer-pass-123")
    # must change the temporary password first
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/profile")
    t = csrf_from(client.get("/profile").text)
    client.post("/profile/password", data={"csrf_token": t, "current": "Viewer-pass-123", "new": "Viewer-pass-456",
                                           "new2": "Viewer-pass-456"})
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/strategies").status_code == 200
    t = csrf_from(client.get("/").text)
    assert client.get("/users").status_code == 403
    assert client.get("/strategies/new").status_code == 403
    assert client.post("/settings", data={"csrf_token": t}).status_code == 403
    assert client.post("/api/kill/global", headers={"X-CSRF-Token": t}).status_code == 403


def test_idle_session_expires(client):
    setup_admin(client)
    login(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    with client.app.state.db.session() as s:
        row = s.query(UserSession).one()
        row.last_seen_at = row.last_seen_at - timedelta(minutes=31)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_logout_destroys_session(client):
    setup_admin(client)
    login(client)
    tok = csrf_from(client.get("/").text)
    client.post("/logout", data={"csrf_token": tok})
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")
    with client.app.state.db.session() as s:
        assert s.query(UserSession).count() == 0


def test_audit_records_logins(client):
    setup_admin(client)
    login(client, password="nope-nope-1")
    login(client)
    page = client.get("/audit").text
    assert "login_failed" in page and ">login<" in page


def test_security_headers(client):
    setup_admin(client)
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
