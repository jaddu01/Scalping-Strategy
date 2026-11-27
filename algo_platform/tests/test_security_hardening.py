"""Security hardening from the pre-production audit: open-redirect checks on ?next=, the dashboard's inline JSON
cannot break out of its <script> block, robots.txt / favicon.ico answer without a login."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.web.render import safe_json
from app.web.routes_auth import _safe_next


@pytest.mark.parametrize("nxt, want", [
    ("/trades", "/trades"),
    ("/strategies/5?date=2026-11-20", "/strategies/5?date=2026-11-20"),
    ("//evil.com", "/"),
    ("/\\evil.com", "/"),
    ("/x\\y", "/"),
    ("https://evil.com", "/"),
    ("evil.com", "/"),
    ("/\tevil", "/"),
    ("/a\r\nSet-Cookie: x=1", "/"),
    ("", "/"),
])
def test_safe_next(nxt, want):
    assert _safe_next(nxt) == want


def test_safe_json_cannot_close_the_script_block():
    data = {"name": "</script><img src=x onerror=alert(1)>", "n": "a & b"}
    out = safe_json(data)
    assert "<" not in out and ">" not in out and "&" not in out
    assert json.loads(out) == data  # still the same data for JSON.parse


def test_robots_and_favicon_without_login(tmp_path):
    from app.main import create_app
    from core.config import AppConfig
    from storage.crypto import Crypto
    from tests.test_auth import setup_admin

    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'w.db'}", master_key=Crypto.generate_key())
    with TestClient(create_app(cfg, start_engine=False), base_url="http://127.0.0.1") as c:
        setup_admin(c)
        r = c.get("/robots.txt")
        assert r.status_code == 200 and "Disallow: /" in r.text
        assert c.get("/favicon.ico").status_code == 204
        assert c.get("/trades", follow_redirects=False).status_code == 303  # everything else still needs a login


def test_login_redirect_drops_a_foreign_next(tmp_path):
    from app.main import create_app
    from app.web import routes_auth
    from core.config import AppConfig
    from storage.crypto import Crypto
    from tests.test_auth import login, setup_admin

    routes_auth._throttle._hits.clear()
    cfg = AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'w.db'}", master_key=Crypto.generate_key())
    with TestClient(create_app(cfg, start_engine=False), base_url="http://127.0.0.1") as c:
        setup_admin(c)
        r = c.get("/login")
        from tests.test_auth import PW, csrf_from

        r = c.post("/login", data={"username": "admin", "password": PW, "csrf_token": csrf_from(r.text),
                                   "next": "/\\evil.com"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert login  # helper import kept for symmetry with the other auth tests
