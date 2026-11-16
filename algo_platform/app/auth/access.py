"""Who may do what (partners).

Roles:
  admin   everything, including other partners' strategies and accounts (for emergencies), users,
          global settings, the data feed and the global kill switch
  trader  a partner: owns broker accounts and strategies, and controls only their own
  viewer  read-only

Each user SEES only their own broker accounts, strategies, trades, orders, P&L and logs. The admin is a user too:
by default they see their own, and can still switch the "Partner" filter to All / one partner (for emergencies).
Changing, starting, stopping or killing something needs `can_control` on its owner.
Broker credentials are never shown to anyone (masked even for the owner).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import or_, select

from app.auth.deps import current_user
from app.auth.security import SessionInfo
from storage.models import BrokerAccount, StrategyInstance, User

ROLES = ("admin", "trader", "viewer")
ROLE_HELP = {"admin": "admin (everything)", "trader": "partner (own accounts and strategies)",
             "viewer": "viewer (read-only)"}


def is_admin(user: SessionInfo | None) -> bool:
    return bool(user and user.role == "admin")


def can_trade(user: SessionInfo | None) -> bool:
    """May own accounts and strategies, run backtests."""
    return bool(user and user.role in ("admin", "trader"))


def can_control(user: SessionInfo | None, owner_user_id: int | None) -> bool:
    """Admins control everything; a partner controls what they own. Unowned items: admins only."""
    if is_admin(user):
        return True
    return bool(user and user.role == "trader" and owner_user_id is not None and owner_user_id == user.user_id)


async def require_trader(user: SessionInfo = Depends(current_user)) -> SessionInfo:
    if not can_trade(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "partner or admin role required")
    return user


def _denied(kind: str) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, f"this {kind} belongs to another partner")


def instance_for_control(request: Request, user: SessionInfo, iid: int) -> StrategyInstance:
    """The strategy row (detached) if the user may change it; 404 / 403 otherwise."""
    with request.app.state.db.session() as s:
        row = s.get(StrategyInstance, iid)
        if row is None:
            raise HTTPException(404, "instance not found")
        s.expunge(row)
    if not can_control(user, row.owner_user_id):
        raise _denied("strategy")
    return row


def account_for_control(request: Request, user: SessionInfo, aid: int) -> BrokerAccount:
    with request.app.state.db.session() as s:
        row = s.get(BrokerAccount, aid)
        if row is None:
            raise HTTPException(404, "account not found")
        s.expunge(row)
    if not can_control(user, row.owner_user_id):
        raise _denied("broker account")
    return row


def usernames(db: Any) -> dict[int, str]:
    with db.session() as s:
        return {u.id: u.username for u in s.scalars(select(User))}


def owner_choices(db: Any) -> list[tuple[int, str]]:
    """Users who can own things (for the admin's "Owner" select)."""
    with db.session() as s:
        return [(u.id, u.username) for u in s.scalars(select(User).where(User.is_active.is_(True),
                                                                          User.role.in_(("admin", "trader")))
                                                     .order_by(User.username))]


def resolve_owner(request_form_value: Any, user: SessionInfo, db: Any, current: int | None = None) -> int | None:
    """Owner for a create/save: partners always own what they make; an admin may pick any partner."""
    if not is_admin(user):
        return user.user_id if current is None else current
    raw = str(request_form_value or "").strip()
    if not raw:
        return current if current is not None else user.user_id
    try:
        uid = int(raw)
    except ValueError:
        return current if current is not None else user.user_id
    return uid if uid in dict(owner_choices(db)) else (current if current is not None else user.user_id)


def owner_filter(request: Request, user: SessionInfo) -> tuple[str, int | None]:
    """Whose items a page shows: (filter value, user id or None = everybody).

    Partners and viewers always see only their own. The admin sees their own by default ("me"); ?owner=all or
    ?owner=<user id> shows everybody / one partner."""
    return _owner_from(request.query_params.get("owner"), user)


def _owner_from(raw: Any, user: SessionInfo) -> tuple[str, int | None]:
    if not is_admin(user):
        return "me", user.user_id
    raw = str(raw or "me").strip()
    if raw == "all":
        return "all", None
    if raw.isdigit():
        return raw, int(raw)
    return "me", user.user_id


@dataclass(frozen=True)
class Scope:
    """What one viewer may see. owner_id None = everything (the admin's "All partners")."""
    owner_id: int | None
    accounts: frozenset[int] = frozenset()
    instances: frozenset[int] = frozenset()
    unowned: bool = False  # also items with no account / strategy (the admin's own view)

    @property
    def everything(self) -> bool:
        return self.owner_id is None

    def account(self, aid: Any) -> bool:
        return self.everything or aid in self.accounts or (self.unowned and not aid)

    def instance(self, iid: Any) -> bool:
        return self.everything or iid in self.instances or (self.unowned and not iid)

    def trade(self, account_id: Any, instance_id: Any) -> bool:
        if self.everything or account_id in self.accounts or instance_id in self.instances:
            return True
        return self.unowned and not account_id and not instance_id

    def event(self, e: dict[str, Any]) -> bool:
        """Market / system events (no account, no strategy) are for everyone."""
        aid, iid = e.get("account_id"), e.get("instance_id")
        if self.everything or (aid is None and iid is None):
            return True
        return aid in self.accounts or iid in self.instances


def scope_for(db: Any, user: SessionInfo, raw_owner: Any = None) -> Scope:
    _, owner_id = _owner_from(raw_owner, user)
    if owner_id is None:
        return Scope(None)
    # the admin's own view also holds unowned items (only an admin can control those)
    own_unowned = is_admin(user) and owner_id == user.user_id
    with db.session() as s:
        def mine(col: Any) -> Any:
            return or_(col == owner_id, col.is_(None)) if own_unowned else col == owner_id
        accs = frozenset(s.scalars(select(BrokerAccount.id).where(mine(BrokerAccount.owner_user_id))))
        insts = frozenset(s.scalars(select(StrategyInstance.id).where(mine(StrategyInstance.owner_user_id))))
    return Scope(owner_id, accs, insts, own_unowned)


def request_scope(request: Request, user: SessionInfo) -> Scope:
    return scope_for(request.app.state.db, user, request.query_params.get("owner"))


def require_visible(user: SessionInfo, owner_user_id: int | None) -> None:
    """404 (not 403: do not even confirm it exists) when a partner opens someone else's item."""
    if not is_admin(user) and (owner_user_id is None or owner_user_id != user.user_id):
        raise HTTPException(404, "not found")


def _totals(accounts: dict[Any, dict[str, Any]]) -> dict[str, Any]:
    return {"gross": round(sum(b.get("gross", 0.0) for b in accounts.values()), 2),
            "net": round(sum(b.get("net", 0.0) for b in accounts.values()), 2),
            "trades": sum(int(b.get("trades", 0)) for b in accounts.values())}


def scope_summary(summary: dict[str, Any], scope: Scope) -> dict[str, Any]:
    """Platform.day_summary() cut down to what the viewer may see (totals recomputed)."""
    if scope.everything:
        return summary
    accs = {k: v for k, v in summary.get("accounts", {}).items() if scope.account(k)}
    return {"instances": {k: v for k, v in summary.get("instances", {}).items() if scope.instance(k)},
            "accounts": accs, "total": _totals(accs)}


def scope_snapshot(snap: dict[str, Any], scope: Scope) -> dict[str, Any]:
    """Platform.snapshot() cut down to what the viewer may see."""
    if scope.everything or not snap:
        return snap
    hidden = [a.get("label") for a in snap.get("accounts", []) if not scope.account(a.get("id"))]
    accounts = [a for a in snap.get("accounts", []) if scope.account(a.get("id"))]
    return {**snap,
            "instances": [i for i in snap.get("instances", []) if scope.instance(i.get("id"))],
            "accounts": accounts,
            "positions": [p for p in snap.get("positions", []) if scope.trade(p.get("account_id"), p.get("instance_id"))],
            "external_positions": [p for p in snap.get("external_positions", []) if scope.account(p.get("account_id"))],
            "banners": [b for b in snap.get("banners", []) if not any(h and str(b.get("text", "")).startswith(h)
                                                                     for h in hidden)],
            "totals": _totals({a.get("id"): a.get("pnl") or {} for a in accounts})}
