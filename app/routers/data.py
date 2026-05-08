"""
app/routers/data.py — Collecte manuelle et lecture des données stockées.
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Activity, DailyMetric, User, get_db
from app.schemas import ActivityOut, CollectRequest, DailyMetricOut
from app.services.collect import collect_user_range

router = APIRouter(tags=["data"])


async def _get_user(db: AsyncSession, name: str) -> User:
    user = (await db.execute(select(User).where(User.name == name))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, f"User '{name}' introuvable.")
    return user


@router.post("/collect")
async def collect(payload: CollectRequest, db: AsyncSession = Depends(get_db)):
    user = await _get_user(db, payload.name)
    if not user.token_json:
        raise HTTPException(400, "Pas de token Garmin pour cet utilisateur.")
    start = payload.start_date or (date.today() - timedelta(days=1))
    end   = payload.end_date   or start
    summary = await collect_user_range(db, user, start, end)
    return {"user": payload.name, "start": start, "end": end, **summary}


@router.get("/users/{name}/daily", response_model=list[DailyMetricOut])
async def get_daily(
    name: str,
    start: Optional[date] = Query(None),
    end:   Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, name)
    q = select(DailyMetric).where(DailyMetric.user_id == user.id)
    if start: q = q.where(DailyMetric.date >= start)
    if end:   q = q.where(DailyMetric.date <= end)
    return (await db.execute(q.order_by(DailyMetric.date))).scalars().all()


@router.get("/users/{name}/activities", response_model=list[ActivityOut])
async def get_activities(
    name: str,
    start:         Optional[date] = Query(None),
    end:           Optional[date] = Query(None),
    activity_type: Optional[str]  = Query(None),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, name)
    q = select(Activity).where(Activity.user_id == user.id)
    if start: q = q.where(Activity.date >= start)
    if end:   q = q.where(Activity.date <= end)
    if activity_type: q = q.where(Activity.activity_type == activity_type)
    return (await db.execute(q.order_by(Activity.date.desc()))).scalars().all()


# ─────────────────────────────────────────────────────────────
# RPE
# ─────────────────────────────────────────────────────────────

class RPEItem(BaseModel):
    activity_id: int
    rpe: int  # 1-10


class RPEPayload(BaseModel):
    name: str
    ratings: list[RPEItem]


@router.get("/users/{name}/activities/recent", response_model=list[ActivityOut])
async def get_recent_activities(
    name: str,
    days: int = Query(3, description="Nombre de jours en arrière"),
    db: AsyncSession = Depends(get_db),
):
    """Retourne les séances des N derniers jours pour affichage RPE."""
    user = await _get_user(db, name)
    since = date.today() - timedelta(days=days)
    q = (
        select(Activity)
        .where(Activity.user_id == user.id)
        .where(Activity.date >= since)
        .order_by(Activity.date.desc())
    )
    return (await db.execute(q)).scalars().all()


@router.post("/rpe")
async def submit_rpe(payload: RPEPayload, db: AsyncSession = Depends(get_db)):
    """Enregistre les notes RPE pour les séances d'un utilisateur."""
    user = await _get_user(db, payload.name)
    updated = 0
    for item in payload.ratings:
        if not (1 <= item.rpe <= 10):
            raise HTTPException(400, f"RPE doit être entre 1 et 10, reçu: {item.rpe}")
        result = await db.execute(
            update(Activity)
            .where(Activity.user_id == user.id)
            .where(Activity.activity_id == item.activity_id)
            .values(rpe=item.rpe)
        )
        updated += result.rowcount
    await db.commit()
    return {"status": "ok", "updated": updated}