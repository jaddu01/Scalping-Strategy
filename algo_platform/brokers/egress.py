"""Per-account outgoing IP (SEBI / Angel One: one static IP per client code).

The server has several public IPs. A small forward proxy on the host (the "IP gateway", squid) has one
port per IP, and each port always sends traffic out from that IP. ALGO_EGRESS_PROXIES maps a public IP
to its gateway port, e.g.

    ALGO_EGRESS_PROXIES="13.126.29.140=http://172.18.0.1:3129, 13.205.116.78=http://172.18.0.1:3130"
    ALGO_EGRESS_DEFAULT_IP="65.0.62.220"     # the server's own IP: used directly, no proxy

An account's "Static public IP" picks its route. Before every login the platform asks
checkip.amazonaws.com (through the same route) which IP the traffic really leaves from, and refuses to
log in if it is not the account's IP — so no order can ever go out from another partner's IP.
Without ALGO_EGRESS_PROXIES (local runs, tests) nothing changes.
"""
from __future__ import annotations

import os

CHECK_URL = "https://checkip.amazonaws.com"


def _routes() -> dict[str, str]:
    raw = os.environ.get("ALGO_EGRESS_PROXIES", "")
    out: dict[str, str] = {}
    for part in raw.replace(";", ",").split(","):
        if "=" in part:
            ip, url = part.split("=", 1)
            if ip.strip() and url.strip():
                out[ip.strip()] = url.strip()
    return out


def enabled() -> bool:
    return bool(_routes())


def default_ip() -> str:
    return os.environ.get("ALGO_EGRESS_DEFAULT_IP", "").strip()


def proxy_for(public_ip: str | None) -> str | None:
    """Gateway URL for an account's static IP; None = go out directly (the server's own IP)."""
    return _routes().get((public_ip or "").strip())


def known_ips() -> list[str]:
    ips = list(_routes())
    if default_ip():
        ips.insert(0, default_ip())
    return ips


def route_problem(public_ip: str | None) -> str | None:
    """Why an account's static IP cannot be used on this server (None = fine)."""
    if not enabled():
        return None
    ip = (public_ip or "").strip() or default_ip()
    if not ip:
        return "set the account's static public IP"
    if ip != default_ip() and ip not in _routes():
        return f"{ip} is not one of this server's IPs ({', '.join(known_ips())})"
    return None


def expected_ip(public_ip: str | None) -> str:
    return (public_ip or "").strip() or default_ip()


def check_egress(public_ip: str | None, timeout: float = 8.0) -> str:
    """The IP this account's traffic actually leaves from (blocking; call in a thread)."""
    import requests

    proxy = proxy_for(public_ip)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.get(CHECK_URL, proxies=proxies, timeout=timeout)
    r.raise_for_status()
    return r.text.strip()
