"""
app/main.py — Application FastAPI, lifespan, scheduler cron.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import AsyncSessionLocal, init_db
from app.routers import data, users, polar, withings, profile
from app.services.collect import collect_all_users_yesterday
from app.logging_config import setup_logging
setup_logging()

from app.routers.session_history_router import router as session_history_router

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

STATIC_DIR = Path(__file__).parent / "static"


async def _daily_job():
    """
    Cron job quotidien — 03:00 UTC
    1. Vérifie et rafraîchit les tokens expirés (re-login préventif)
    2. Collecte les métriques J-1 pour tous les utilisateurs
    """
    log.info("=== CRON START ===")
    async with AsyncSessionLocal() as db:
        # Re-login préventif avant la collecte
        try:
            from app.services.garmin_auth import check_and_refresh_tokens
            await check_and_refresh_tokens(db)
        except Exception as e:
            log.error(f"Erreur vérification tokens : {e}")
 
        # Collecte normale
        from app.services.collect import collect_all_users_yesterday
        await collect_all_users_yesterday(db)
    log.info("=== CRON END ===")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    await init_db()
    log.info("✓ DB prête")

    scheduler.add_job(
        _daily_job,
        CronTrigger(hour=settings.collect_hour, minute=settings.collect_minute, timezone="UTC"),
        id="daily_collect",
        replace_existing=True,
    )
    scheduler.start()
    log.info(f"✓ Cron démarré — collecte à {settings.collect_hour:02d}:{settings.collect_minute:02d} UTC")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="CRONOS Backend", version="0.1.0", lifespan=lifespan)

app.include_router(users.router)
app.include_router(data.router)
app.include_router(polar.router)
app.include_router(withings.router)
app.include_router(profile.router)
app.include_router(session_history_router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", include_in_schema=False)
async def serve_landing():
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/connect", include_in_schema=False)
async def serve_connect():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}

@app.get("/cronos", include_in_schema=False)
async def serve_cronos():
    return FileResponse(STATIC_DIR / "cronos.html")