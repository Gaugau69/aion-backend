"""
app/services/collect.py — Orchestration de la collecte et upsert en DB.

Supporte Garmin, Polar et Withings — détecte automatiquement le provider depuis le token.
"""

import json
import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Activity, DailyMetric, User
from app.services.garmin_auth import get_api
from app.services.garmin_parse import (
    parse_activities, parse_body_battery, parse_heart_rate,
    parse_hrv, parse_sleep, parse_stats, parse_steps, parse_stress,
)
from app.services.polar_auth import get_polar_api_headers
from app.services.polar_parse import collect_activities_polar, collect_day_polar
from app.services.withings_auth import get_withings_headers, get_withings_userid
from app.services.withings_parse import collect_activities_withings, collect_day_withings

log = logging.getLogger(__name__)

_GARMIN_PARSERS = [
    parse_sleep, parse_heart_rate, parse_hrv,
    parse_stress, parse_steps, parse_body_battery, parse_stats,
]


def _get_provider(user: User) -> str:
    """Détecte le provider (garmin/polar/withings) depuis le token."""
    if not user.token_json:
        return "unknown"
    try:
        token_data = json.loads(user.token_json)
        return token_data.get("provider", "garmin")
    except Exception:
        return "garmin"


def _safe_upsert_row(row: dict, exclude_keys: tuple) -> dict:
    """
    Retourne un dict de mise à jour sans les clés exclues et non vide.
    Évite le ValueError: set parameter dictionary must not be empty.
    """
    set_dict = {k: v for k, v in row.items() if k not in exclude_keys}
    return set_dict if set_dict else {"user_id": row.get("user_id")}


async def collect_user_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    """Collecte toutes les métriques d'un user entre start et end, upsert en DB."""
    provider = _get_provider(user)
    log.info(f"[{user.name}] provider: {provider}")

    if provider == "polar":
        return await _collect_polar_range(db, user, start, end)
    elif provider == "withings":
        return await _collect_withings_range(db, user, start, end)
    else:
        return await _collect_garmin_range(db, user, start, end)


# ─────────────────────────────────────────────────────────────
# Garmin
# ─────────────────────────────────────────────────────────────

async def _collect_garmin_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    api = await get_api(db, user)
    if not api:
        return {"status": "error", "reason": "token invalide"}

    days_ok = 0
    acts_ok = 0
    current = start

    while current <= end:
        log.info(f"[{user.name}] collecting {current}")

        row = {"user_id": user.id, "date": current}
        for parser in _GARMIN_PARSERS:
            row.update(parser(api, current))

        set_dict = _safe_upsert_row(row, ("user_id", "date"))
        await db.execute(
            pg_insert(DailyMetric)
            .values(**row)
            .on_conflict_do_update(
                constraint="uq_user_date",
                set_=set_dict,
            )
        )
        days_ok += 1

        for act in parse_activities(api, current):
            act_row = {"user_id": user.id, "date": current, **act}
            act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
            await db.execute(
                pg_insert(Activity)
                .values(**act_row)
                .on_conflict_do_update(
                    constraint="uq_user_activity",
                    set_=act_set,
                )
            )
            acts_ok += 1

        await db.commit()
        current += timedelta(days=1)

    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Polar
# ─────────────────────────────────────────────────────────────

async def _collect_polar_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    headers = await get_polar_api_headers(user)
    if not headers:
        return {"status": "error", "reason": "token Polar invalide"}

    try:
        token_data = json.loads(user.token_json)
        polar_user_id = token_data.get("polar_user_id", "")
    except Exception:
        return {"status": "error", "reason": "token Polar mal formé"}

    if not polar_user_id:
        return {"status": "error", "reason": "polar_user_id manquant"}

    days_ok = 0
    acts_ok = 0
    current = start

    while current <= end:
        log.info(f"[{user.name}] collecting Polar {current}")

        metrics = await collect_day_polar(headers, polar_user_id, current)
        row = {"user_id": user.id, "date": current, **metrics}

        set_dict = _safe_upsert_row(row, ("user_id", "date"))
        await db.execute(
            pg_insert(DailyMetric)
            .values(**row)
            .on_conflict_do_update(
                constraint="uq_user_date",
                set_=set_dict,
            )
        )
        days_ok += 1

        activities = await collect_activities_polar(headers, polar_user_id, current)
        for act in activities:
            act_row = {"user_id": user.id, "date": current, **act}
            act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
            await db.execute(
                pg_insert(Activity)
                .values(**act_row)
                .on_conflict_do_update(
                    constraint="uq_user_activity",
                    set_=act_set,
                )
            )
            acts_ok += 1

        await db.commit()
        current += timedelta(days=1)

    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Withings
# ─────────────────────────────────────────────────────────────

async def _collect_withings_range(db: AsyncSession, user: User, start: date, end: date) -> dict:
    headers = await get_withings_headers(user)
    if not headers:
        return {"status": "error", "reason": "token Withings invalide"}

    days_ok = 0
    acts_ok = 0
    current = start

    while current <= end:
        log.info(f"[{user.name}] collecting Withings {current}")

        metrics = await collect_day_withings(headers, current)
        row = {"user_id": user.id, "date": current, **metrics}

        set_dict = _safe_upsert_row(row, ("user_id", "date"))
        await db.execute(
            pg_insert(DailyMetric)
            .values(**row)
            .on_conflict_do_update(
                constraint="uq_user_date",
                set_=set_dict,
            )
        )
        days_ok += 1

        activities = await collect_activities_withings(headers, current)
        for act in activities:
            act_row = {"user_id": user.id, "date": current, **act}
            act_set = _safe_upsert_row(act_row, ("user_id", "activity_id"))
            await db.execute(
                pg_insert(Activity)
                .values(**act_row)
                .on_conflict_do_update(
                    constraint="uq_user_activity",
                    set_=act_set,
                )
            )
            acts_ok += 1

        await db.commit()
        current += timedelta(days=1)

    return {"status": "ok", "days": days_ok, "activities": acts_ok}


# ─────────────────────────────────────────────────────────────
# Cron job
# ─────────────────────────────────────────────────────────────

async def collect_all_users_yesterday(db: AsyncSession):
    """Cron job — collecte J-1 pour tous les users enregistrés."""
    yesterday = date.today() - timedelta(days=1)
    users = (await db.execute(select(User))).scalars().all()
    log.info(f"Cron: {yesterday} — {len(users)} user(s)")
    for user in users:
        try:
            summary = await collect_user_range(db, user, yesterday, yesterday)
            log.info(f"[{user.name}] {summary}")
        except Exception as e:
            log.error(f"[{user.name}] Erreur collecte: {e}")
            # ── NOTIFICATION EMAIL TOKEN EXPIRÉ — À ACTIVER AU LANCEMENT ──
            # if "401" in str(e) or "token invalide" in str(e):
            #     await _notify_token_expired(user.name, user.email)

# ── À ACTIVER AU LANCEMENT ──
# async def _notify_token_expired(name: str, email: str):
#     """Envoie un email quand le token Garmin/Polar expire."""
#     from app.services.email import send_email  # à brancher sur le service d'Antoine
#     await send_email(
#         to=email,
#         subject="Peakflow — Reconnexion requise",
#         body=f"Bonjour {name},\n\nTon accès montre a expiré.\n"
#              f"Relance l'app Peakflow et reconnecte-toi :\n"
#              f"→ https://peakflow-technologies.com/cronos\n\n"
#              f"L'équipe Peakflow"
#     )