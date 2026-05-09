"""
app/routers/data.py — Collecte manuelle et lecture des données stockées.
"""
import numpy as np
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

# ─────────────────────────────────────────────────────────────
# RECOMMEND — Recommandation de séances CRONOS
# ─────────────────────────────────────────────────────────────

# Ajoute en haut de data.py :
# import numpy as np

SESSIONS = [
    {"id": 0,  "name": "Récupération active",    "category": "recuperation", "intensity": 0.2, "duration_min": 40,  "distance_km": 6,  "description": "Footing très lent Z1, conversations possibles", "example": "40min à 6:30/km"},
    {"id": 1,  "name": "Sortie longue Z2",        "category": "endurance",    "intensity": 0.4, "duration_min": 120, "distance_km": 20, "description": "Course longue en zone aérobie, allure confort", "example": "2h à 5:30/km"},
    {"id": 2,  "name": "Sortie longue trail",     "category": "endurance",    "intensity": 0.4, "duration_min": 150, "distance_km": 25, "description": "Sortie longue en nature avec dénivelé", "example": "2h30 avec 800m D+"},
    {"id": 3,  "name": "Tempo continu",           "category": "intensite",    "intensity": 0.7, "duration_min": 50,  "distance_km": 10, "description": "Course soutenue au seuil anaérobie", "example": "50min à 4:20/km"},
    {"id": 4,  "name": "Fractionné court",        "category": "intensite",    "intensity": 0.9, "duration_min": 55,  "distance_km": 10, "description": "Répétitions courtes à haute intensité", "example": "10x400m avec récup 90s"},
    {"id": 5,  "name": "Fractionné long",         "category": "intensite",    "intensity": 0.8, "duration_min": 60,  "distance_km": 12, "description": "Répétitions longues au seuil", "example": "5x1000m avec récup 2min"},
    {"id": 6,  "name": "Côtes",                   "category": "force",        "intensity": 0.85,"duration_min": 50,  "distance_km": 8,  "description": "Répétitions en montée pour la force", "example": "10x200m de côte"},
    {"id": 7,  "name": "Fartlek",                 "category": "intensite",    "intensity": 0.7, "duration_min": 55,  "distance_km": 10, "description": "Variations libres d'allure", "example": "55min avec accélérations libres"},
    {"id": 8,  "name": "Progression",             "category": "endurance",    "intensity": 0.6, "duration_min": 60,  "distance_km": 12, "description": "Course avec augmentation progressive de l'allure", "example": "60min en finissant vite"},
    {"id": 9,  "name": "Endurance fondamentale",  "category": "endurance",    "intensity": 0.5, "duration_min": 75,  "distance_km": 13, "description": "Course à allure modérée Z2/Z3", "example": "75min à 5:00/km"},
    {"id": 10, "name": "Spécifique marathon",     "category": "specifique",   "intensity": 0.7, "duration_min": 90,  "distance_km": 18, "description": "Allure marathon en condition de course", "example": "90min à allure objectif"},
    {"id": 11, "name": "Spécifique semi",         "category": "specifique",   "intensity": 0.75,"duration_min": 70,  "distance_km": 14, "description": "Allure semi-marathon en condition de course", "example": "70min à allure objectif"},
    {"id": 12, "name": "Spécifique 10km",         "category": "specifique",   "intensity": 0.8, "duration_min": 55,  "distance_km": 11, "description": "Allure 10km en condition de course", "example": "55min à allure objectif"},
    {"id": 13, "name": "Trail technique",         "category": "specifique",   "intensity": 0.6, "duration_min": 90,  "distance_km": 15, "description": "Travail technique en terrain varié", "example": "90min sur sentiers techniques"},
    {"id": 14, "name": "Ultra endurance",         "category": "endurance",    "intensity": 0.4, "duration_min": 240, "distance_km": 40, "description": "Sortie très longue pour ultra-trail", "example": "4h avec 1500m D+"},
    {"id": 15, "name": "Gainage et renforcement", "category": "force",        "intensity": 0.4, "duration_min": 45,  "distance_km": 0,  "description": "Séance de renforcement musculaire", "example": "45min PPG/gainage"},
    {"id": 16, "name": "Mobilité et récupération","category": "recuperation", "intensity": 0.1, "duration_min": 30,  "distance_km": 0,  "description": "Stretching, yoga, mobilité", "example": "30min yoga/stretching"},
    {"id": 17, "name": "Seuil lactique",          "category": "intensite",    "intensity": 0.75,"duration_min": 55,  "distance_km": 11, "description": "Course au seuil lactique", "example": "20min échauffement + 3x10min seuil"},
    {"id": 18, "name": "VO2max",                  "category": "intensite",    "intensity": 0.95,"duration_min": 50,  "distance_km": 10, "description": "Intervalles à VO2max", "example": "6x3min à 95% FCmax"},
    {"id": 19, "name": "Cross-training",          "category": "recuperation", "intensity": 0.2, "duration_min": 45,  "distance_km": 0,  "description": "Natation, vélo, elliptique", "example": "45min vélo ou natation"},
]

RECOVERY_COST = [0.1, 0.4, 0.5, 0.6, 0.7, 0.7, 0.7, 0.5, 0.5, 0.3, 0.6, 0.6, 0.6, 0.5, 0.8, 0.2, 0.0, 0.65, 0.8, 0.1]


def _compute_recovery(metrics: list) -> tuple[float, dict]:
    """Calcule le score de récupération depuis les métriques DB."""
    import numpy as np

    latest = metrics[0]
    hrv_values = [float(m.hrv_last_night) for m in metrics if m.hrv_last_night]
    hrv_mean  = float(np.median(hrv_values)) if hrv_values else None
    hrv_today = float(latest.hrv_last_night) if latest.hrv_last_night else None
    sleep_today = float(latest.sleep_score) if latest.sleep_score else None
    bb_today = float(latest.body_battery_charged) if latest.body_battery_charged else None

    recovery = 0.5
    n_signals = 0

    if hrv_today and hrv_mean and hrv_mean > 0:
        hrv_score = min(hrv_today / hrv_mean, 1.5) / 1.5
        recovery += (hrv_score - 0.5) * 0.5
        n_signals += 1

    if sleep_today:
        recovery += (sleep_today / 100 - 0.5) * 0.3
        n_signals += 1

    if bb_today:
        recovery += (bb_today / 100 - 0.5) * 0.2
        n_signals += 1

    recovery = max(0.05, min(0.95, recovery))

    signals = {
        "hrv_today":    round(hrv_today, 1) if hrv_today else None,
        "hrv_mean":     round(hrv_mean, 1)  if hrv_mean  else None,
        "sleep_score":  int(sleep_today)    if sleep_today else None,
        "body_battery": int(bb_today)       if bb_today  else None,
        "n_signals":    n_signals,
    }
    return recovery, signals


def _rank_sessions(recovery: float, top_k: int) -> list[dict]:
    """Classe les séances par pertinence selon le score de récupération."""
    scored = []
    for i, session in enumerate(SESSIONS):
        intensity = session["intensity"]
        cost = RECOVERY_COST[i]

        if recovery >= 0.7:
            score = 1.0 - abs(intensity - 0.75) * 0.6
        elif recovery >= 0.5:
            score = 1.0 - abs(intensity - 0.5) * 0.8
        elif recovery >= 0.35:
            score = 1.0 - abs(intensity - 0.3) * 1.0
        else:
            score = 1.0 - abs(intensity - 0.15) * 1.2

        if recovery < 0.4 and cost > 0.6:
            score *= 0.4
        if recovery < 0.4 and session["category"] == "recuperation":
            score = min(0.95, score * 1.3)

        scored.append((max(0.05, min(0.95, score)), session))

    scored.sort(key=lambda x: x[0], reverse=True)

    return [
        {
            "rank":         i + 1,
            "session_id":   s["id"],
            "name":         s["name"],
            "score":        round(sc * 100, 1),
            "category":     s["category"],
            "description":  s["description"],
            "example":      s["example"],
            "intensity":    s["intensity"],
            "duration_min": s["duration_min"],
            "distance_km":  s["distance_km"],
        }
        for i, (sc, s) in enumerate(scored[:top_k])
    ]


@router.get("/users/{name}/recommend")
async def recommend_sessions(
    name: str,
    top_k: int = Query(5, ge=1, le=10, description="Nombre de séances à recommander"),
    db: AsyncSession = Depends(get_db),
):
    """
    Recommande les top_k séances d'entraînement pour aujourd'hui.
    Basé sur HRV, sleep score et body battery des 14 derniers jours.
    """
    user = await _get_user(db, name)

    # Récupère les 15 derniers jours
    since = date.today() - timedelta(days=15)
    metrics = (await db.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user.id)
        .where(DailyMetric.date >= since)
        .order_by(DailyMetric.date.desc())
    )).scalars().all()

    if not metrics:
        raise HTTPException(400, "Pas de données disponibles pour cet utilisateur.")

    recovery, signals = _compute_recovery(metrics)
    recommendations   = _rank_sessions(recovery, top_k)

    return {
        "user":  name,
        "date":  date.today().isoformat(),
        "recovery": {
            "score": round(recovery * 100, 1),
            "level": (
                "Excellente" if recovery >= 0.75 else
                "Bonne"      if recovery >= 0.55 else
                "Moyenne"    if recovery >= 0.4  else
                "Faible"
            ),
            **signals,
        },
        "recommendations": recommendations,
    }