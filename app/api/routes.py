from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from app.config import settings
import httpx
import math
import random
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/route", tags=["route"])

GRAPHHOPPER_API_KEY = settings.graphhopper_api_key
GRAPHHOPPER_URL = "https://graphhopper.com/api/1/route"


class LoopResponse(BaseModel):
    coordinates: list[list[float]]
    distance_km: float
    duration_min: float
    profile: str


def _offset_point(lat: float, lng: float, distance_km: float, bearing_deg: float):
    """Calcule un point à `distance_km` au cap `bearing_deg` depuis (lat, lng)."""
    R = 6371.0  # rayon terrestre en km
    bearing = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lng1 = math.radians(lng)
    angular_dist = distance_km / R

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_dist)
        + math.cos(lat1) * math.sin(angular_dist) * math.cos(bearing)
    )
    lng2 = lng1 + math.atan2(
        math.sin(bearing) * math.sin(angular_dist) * math.cos(lat1),
        math.cos(angular_dist) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lng2)


@router.get("/loop", response_model=LoopResponse)
async def generate_loop(
    lat: float = Query(...),
    lng: float = Query(...),
    distance_km: float = Query(..., gt=0, le=100),
    profile: str = Query("foot", regex="^(foot|bike)$"),
    seed: int = Query(0),
):
    """Génère une boucle approximative : aller jusqu'à un point distant, puis retour."""
    if not GRAPHHOPPER_API_KEY:
        raise HTTPException(500, "GRAPHHOPPER_API_KEY non configurée")

    # On vise un point à environ distance/3 en distance à vol d'oiseau
    # (le trajet réel sur routes fera ~2.5-3x la distance à vol d'oiseau pour un AR)
    target_straight_km = distance_km / 3.0

    # Cap aléatoire mais déterministe (selon seed)
    rng = random.Random(seed if seed else random.randint(1, 100000))
    bearing_out = rng.uniform(0, 360)
    # Détour : pour le retour on passe par un point décalé pour éviter le même chemin
    bearing_detour = (bearing_out + rng.choice([90, -90, 60, -60, 120, -120])) % 360

    target_lat, target_lng = _offset_point(lat, lng, target_straight_km, bearing_out)
    detour_lat, detour_lng = _offset_point(lat, lng, target_straight_km * 0.7, bearing_detour)

    # Construction de la requête : 3 waypoints (départ → aller → détour → retour au départ)
    params = [
        ("profile", profile),
        ("point", f"{lat},{lng}"),
        ("point", f"{target_lat},{target_lng}"),
        ("point", f"{detour_lat},{detour_lng}"),
        ("point", f"{lat},{lng}"),
        ("points_encoded", "false"),
        ("instructions", "false"),
        ("key", GRAPHHOPPER_API_KEY),
    ]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(GRAPHHOPPER_URL, params=params)
    except httpx.RequestError as e:
        logger.error(f"GraphHopper request failed: {e}")
        raise HTTPException(503, "Service de routing indisponible")

    if r.status_code != 200:
        logger.warning(f"GraphHopper returned {r.status_code}: {r.text[:200]}")
        raise HTTPException(r.status_code, f"GraphHopper error: {r.text[:200]}")

    data = r.json()
    paths = data.get("paths", [])
    if not paths:
        raise HTTPException(404, "Aucun parcours trouvé")

    path = paths[0]
    coords = [[pt[1], pt[0]] for pt in path["points"]["coordinates"]]

    return LoopResponse(
        coordinates=coords,
        distance_km=round(path["distance"] / 1000, 2),
        duration_min=round(path["time"] / 60000, 1),
        profile=profile,
    )