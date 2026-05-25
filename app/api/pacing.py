"""
app/api/pacing.py — Calcule l'allure de l'athlète à partir de ses activités récentes,
avec fallback sur les best times du profil.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, and_
from datetime import date, timedelta
from typing import Optional
import logging

from app.db import AsyncSessionLocal, User, Activity, AthleteProfile

logger = logging.getLogger(__name__)
router = APIRouter(tags=["pacing"])


class PacingResponse(BaseModel):
    avg_pace_min_per_km: Optional[float]
    n_activities: int
    source: str  # 'recent_activities' | 'best_times' | 'unavailable'
    estimated_paces: Optional[dict]


def _pace_from_speed(speed_kmh: float) -> float:
    """km/h → min/km."""
    if not speed_kmh or speed_kmh <= 0:
        return 0.0
    return 60.0 / speed_kmh


def _riegel_pace(ref_time_min: float, ref_dist_km: float, target_dist_km: float) -> float:
    """Formule de Riegel : t2 = t1 × (d2/d1)^1.06."""
    if ref_time_min <= 0 or ref_dist_km <= 0:
        return 0.0
    predicted = ref_time_min * (target_dist_km / ref_dist_km) ** 1.06
    return predicted / target_dist_km


def _category_paces(base_pace: float) -> dict:
    """Allures par catégorie de séance, à partir de l'allure footing de base."""
    return {
        "recuperation": round(base_pace * 1.20, 2),  # Z1 : très lent
        "endurance":    round(base_pace * 1.08, 2),  # Z2 : footing facile
        "specifique":   round(base_pace * 0.92, 2),  # tempo / seuil
        "intensite":    round(base_pace * 0.82, 2),  # VO2max / fractionné
        "force":        None,
        "repos":        None,
    }


@router.get("/users/{name}/pacing", response_model=PacingResponse)
async def get_user_pacing(name: str):
    async with AsyncSessionLocal() as session:
        # Récupère l'utilisateur
        user_res = await session.execute(select(User).where(User.name == name))
        user = user_res.scalar_one_or_none()
        if not user:
            raise HTTPException(404, "User not found")

        # ── Stratégie 1 : moyenne pondérée sur les running récents ──
        cutoff = date.today() - timedelta(days=90)
        acts_res = await session.execute(
            select(Activity).where(
                and_(
                    Activity.user_id == user.id,
                    Activity.activity_type.in_([
                        "running", "run", "trail_running", "treadmill_running", "indoor_running"
                    ]),
                    Activity.distance_km >= 2.0,    # filtre warm-ups et bidouilles
                    Activity.distance_km <= 50.0,   # filtre ultras (allure non représentative)
                    Activity.avg_speed_kmh > 5.0,   # filtre marche
                    Activity.avg_speed_kmh < 25.0,  # filtre vélo mal classé
                    Activity.date >= cutoff,
                )
            ).order_by(Activity.date.desc()).limit(20)
        )
        activities = acts_res.scalars().all()

        if len(activities) >= 3:
            today = date.today()
            total_weight = 0.0
            weighted_sum = 0.0
            for act in activities:
                days_ago = (today - act.date).days if act.date else 90
                weight = 0.5 ** (days_ago / 30)  # demi-vie 30 jours
                pace = _pace_from_speed(act.avg_speed_kmh or 0)
                if pace > 0:
                    weighted_sum += pace * weight
                    total_weight += weight

            if total_weight > 0:
                avg_pace = weighted_sum / total_weight
                return PacingResponse(
                    avg_pace_min_per_km=round(avg_pace, 2),
                    n_activities=len(activities),
                    source="recent_activities",
                    estimated_paces=_category_paces(avg_pace),
                )

        # ── Stratégie 2 : fallback sur les best times (Riegel) ──
        prof_res = await session.execute(
            select(AthleteProfile).where(AthleteProfile.user_id == user.id)
        )
        profile = prof_res.scalar_one_or_none()

        if profile:
            # Priorité : 10k > semi > 5k > marathon
            reference = None
            if profile.best_10k_min:
                reference = (profile.best_10k_min, 10.0)
            elif profile.best_half_min:
                reference = (profile.best_half_min, 21.0975)
            elif profile.best_5k_min:
                reference = (profile.best_5k_min, 5.0)
            elif profile.best_marathon_min:
                reference = (profile.best_marathon_min, 42.195)

            if reference:
                ref_time, ref_dist = reference
                race_pace = _riegel_pace(ref_time, ref_dist, 10.0)
                # Best times = perf de compétition ; on adoucit pour avoir une allure footing
                training_pace = race_pace * 1.15
                return PacingResponse(
                    avg_pace_min_per_km=round(training_pace, 2),
                    n_activities=0,
                    source="best_times",
                    estimated_paces=_category_paces(training_pace),
                )

        # ── Rien ──
        return PacingResponse(
            avg_pace_min_per_km=None,
            n_activities=len(activities),
            source="unavailable",
            estimated_paces=None,
        )