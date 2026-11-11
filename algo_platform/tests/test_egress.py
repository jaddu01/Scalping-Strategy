"""Per-account outgoing IP: routing, the login IP check and the account form rules."""
from __future__ import annotations

import pytest

from brokers import egress
from brokers.angelone import AngelOneAdapter, AngelOneCredentials, _ws_proxy
from brokers.base import BrokerError

ROUTES = "13.126.29.140=http://172.18.0.1:3129, 13.205.116.78=http://172.18.0.1:3130"


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("ALGO_EGRESS_PROXIES", ROUTES)
    monkeypatch.setenv("ALGO_EGRESS_DEFAULT_IP", "65.0.62.220")


def creds(ip: str = "") -> AngelOneCredentials:
    return AngelOneCredentials(client_id="A1", api_key="k", pin="1234", totp_secret="JBSWY3DPEHPK3PXP",
                               client_public_ip=ip)


def test_routes(gateway):
    assert egress.proxy_for("13.126.29.140") == "http://172.18.0.1:3129"
    assert egress.proxy_for("65.0.62.220") is None and egress.proxy_for("") is None  # the server's own IP: direct
    assert egress.route_problem("") is None and egress.route_problem("65.0.62.220") is None
    assert "not one of this server's IPs" in egress.route_problem("1.2.3.4")
    assert _ws_proxy("13.205.116.78") == {"http_proxy_host": "172.18.0.1", "http_proxy_port": 3130,
                                          "proxy_type": "http"}
    assert _ws_proxy("65.0.62.220") == {}


def test_off_without_config(monkeypatch):
    monkeypatch.delenv("ALGO_EGRESS_PROXIES", raising=False)
    assert not egress.enabled() and egress.route_problem("1.2.3.4") is None and egress.proxy_for("1.2.3.4") is None


def test_smartconnect_gets_the_partners_proxy(gateway):
    api = AngelOneAdapter(creds("13.126.29.140"))._new_api()
    assert api.proxies == {"http": "http://172.18.0.1:3129", "https": "http://172.18.0.1:3129"}
    assert api.clientPublicIP == "13.126.29.140"
    assert AngelOneAdapter(creds("65.0.62.220"))._new_api().proxies == {}


async def test_login_refused_when_traffic_leaves_from_another_ip(gateway, monkeypatch):
    seen = []

    def fake_check(ip, timeout=8.0):
        seen.append(ip)
        return "65.0.62.220"  # e.g. the gateway is misconfigured and traffic leaves from the main IP

    monkeypatch.setattr(egress, "check_egress", fake_check)
    ad = AngelOneAdapter(creds("13.126.29.140"))
    with pytest.raises(BrokerError, match="not this account's static IP 13.126.29.140"):
        await ad.login()
    assert seen == ["13.126.29.140"] and not ad._connected


async def test_login_refused_when_gateway_is_down(gateway, monkeypatch):
    def down(ip, timeout=8.0):
        raise ConnectionError("proxy refused")

    monkeypatch.setattr(egress, "check_egress", down)
    with pytest.raises(BrokerError, match="IP gateway"):
        await AngelOneAdapter(creds("13.205.116.78")).login()


from tests.test_rbac import as_user, client  # noqa: E402,F401  (fixture)


def _angel(c, t, label, ip):
    return c.post("/accounts/new?broker=angelone", data={
        "csrf_token": t, "label": label, "cred.client_id": label.replace(" ", ""), "cred.api_key": "KEY123456",
        "cred.pin": "1234", "cred.totp_secret": "JBSWY3DPEHPK3PXP", "cred.client_public_ip": ip,
        "risk.max_open_positions": "5", "risk.max_daily_loss": "0", "risk.max_orders_per_day": "500"},
        follow_redirects=False)


def test_account_form_one_static_ip_per_client_code(client, gateway):  # noqa: F811
    t = as_user(client, "bina")
    assert _angel(client, t, "Bina Angel", "13.126.29.140").status_code == 303
    r = _angel(client, t, "Bina Second", "13.126.29.140")
    assert r.status_code == 400 and "already used by Bina Angel" in r.text
    r = _angel(client, t, "Bina Third", "8.8.8.8")
    assert r.status_code == 400 and "not one of this server&#39;s IPs" in r.text
    t = as_user(client, "chetan")
    assert _angel(client, t, "Chetan Angel", "13.205.116.78").status_code == 303
    assert _angel(client, t, "Chetan Main", "").status_code == 303  # blank = the server's own IP
    r = _angel(client, t, "Another Main", "65.0.62.220")
    assert r.status_code == 400 and "already used by Chetan Main" in r.text
