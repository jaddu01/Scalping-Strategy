from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.auth.deps import SessionInfo, current_user
from app.web.render import render
from core.events import Category, Level
from storage.models import BrokerAccount, StrategyInstance

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, user: SessionInfo = Depends(current_user)) -> HTMLResponse:
    with request.app.state.db.session() as s:
        instances = [(i.id, i.name) for i in s.scalars(select(StrategyInstance).order_by(StrategyInstance.name))]
        accounts = [(a.id, a.label) for a in s.scalars(select(BrokerAccount).order_by(BrokerAccount.label))]
    return render(request, "logs.html", {"instances": instances, "accounts": accounts,
                                         "levels": [lv.value for lv in Level], "categories": [c.value for c in Category],
                                         "q": dict(request.query_params)})
