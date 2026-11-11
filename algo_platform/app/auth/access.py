"""Who may do what (partners).

Roles:
  admin   everything, including other partners' strategies and accounts (for emergencies), users,
          global settings, the data feed and the global kill switch
  trader  a partner: owns broker accounts and strategies, and controls only their own
  viewer  read-only

Everyone who is logged in can SEE all strategies, trades and P&L (the partners share results).
Changing, starting, stopping or killing something needs `can_control` on its owner.
Broker credentials are never shown to anyone (masked even for the owner).
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

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
    """The ?owner= list filter: "all" (default), "me", or a user id. Returns (value, user id or None)."""
    raw = (request.query_params.get("owner") or "all").strip()
    if raw == "me":
        return raw, user.user_id
    if raw.isdigit():
        return raw, int(raw)
    return "all", None
