"""Nearby places, using the *device's* location rather than the server's.

The whole point: the server sits in an Oracle datacentre, so its IP says nothing
about where you are. Every request from the app carries the browser's Geolocation
coordinates, which land in ToolContext — so "barbers near me" resolves against
wherever your phone actually is.

Backends are OpenStreetMap: Overpass for POI search, Nominatim for geocoding.
Both are free with no key. We honour their usage policy with a real User-Agent
and modest query sizes.
"""

from __future__ import annotations

import logging
import math

import httpx

from ..config import get_settings
from .base import ToolContext, ToolResult, registry

log = logging.getLogger(__name__)

# Maps everyday words to OpenStreetMap tags. The model passes a category, not raw tags.
CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "haircut": [("shop", "hairdresser"), ("shop", "barber")],
    "barber": [("shop", "barber"), ("shop", "hairdresser")],
    "restaurant": [("amenity", "restaurant")],
    "cafe": [("amenity", "cafe")],
    "bar": [("amenity", "bar"), ("amenity", "pub")],
    "pharmacy": [("amenity", "pharmacy")],
    "hospital": [("amenity", "hospital")],
    "doctor": [("amenity", "doctors")],
    "dentist": [("amenity", "dentist")],
    "gym": [("leisure", "fitness_centre")],
    "supermarket": [("shop", "supermarket")],
    "grocery": [("shop", "supermarket"), ("shop", "convenience")],
    "bank": [("amenity", "bank")],
    "atm": [("amenity", "atm")],
    "fuel": [("amenity", "fuel")],
    "gas": [("amenity", "fuel")],
    "parking": [("amenity", "parking")],
    "hotel": [("tourism", "hotel")],
    "laundry": [("shop", "laundry"), ("shop", "dry_cleaning")],
    "car_repair": [("shop", "car_repair")],
    "post_office": [("amenity", "post_office")],
    "spa": [("leisure", "spa"), ("shop", "beauty")],
    "nails": [("shop", "beauty")],
    "bookstore": [("shop", "books")],
    "hardware": [("shop", "hardware"), ("shop", "doityourself")],
}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _build_overpass_query(lat: float, lon: float, radius_m: int, tags: list[tuple[str, str]]) -> str:
    clauses = []
    for key, value in tags:
        for element in ("node", "way"):
            clauses.append(f'{element}["{key}"="{value}"](around:{radius_m},{lat},{lon});')
    return f"[out:json][timeout:25];({''.join(clauses)});out center tags 60;"


def _format_hours(tags: dict) -> str:
    return tags.get("opening_hours", "") or ""


def _address(tags: dict) -> str:
    parts = [
        tags.get("addr:housenumber", ""),
        tags.get("addr:street", ""),
        tags.get("addr:city", ""),
        tags.get("addr:postcode", ""),
    ]
    return " ".join(p for p in parts if p).strip()


@registry.tool(
    name="places_search",
    description=(
        "Find businesses and points of interest near the user's CURRENT device location. "
        "Use this whenever the user wants something 'nearby', 'near me', or 'around here' — "
        "for example when they mention wanting a haircut, food, a pharmacy or a gym. Returns "
        "name, distance, address, phone and website so you can offer concrete options and "
        "then book one into their calendar."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": (
                    "What to look for. Prefer a known category: "
                    + ", ".join(sorted(CATEGORIES))
                    + ". Any other word is matched against place names."
                ),
            },
            "radius_km": {"type": "number", "description": "Search radius in km. Default 3, max 25."},
            "limit": {"type": "integer", "description": "Max results. Default 8."},
            "near": {
                "type": "string",
                "description": (
                    "Optional place name to search around INSTEAD of the device location. "
                    "Only use if the user explicitly names a different city or address."
                ),
            },
        },
        "required": ["category"],
    },
    needs_location=True,
    tags=["places", "location"],
)
async def places_search(
    category: str,
    radius_km: float = 3.0,
    limit: int = 8,
    near: str = "",
    ctx: ToolContext = None,
):
    settings = get_settings()
    lat, lon, origin = ctx.lat, ctx.lon, "your device location"

    if near:
        geo = await _geocode(near)
        if geo is None:
            return ToolResult.fail(f"Could not find a place called '{near}'.")
        lat, lon, origin = geo["lat"], geo["lon"], geo["name"]

    radius_m = int(max(0.2, min(radius_km, 25.0)) * 1000)
    key = category.lower().strip().replace(" ", "_")
    tags = CATEGORIES.get(key)

    if tags:
        query = _build_overpass_query(lat, lon, radius_m, tags)
    else:
        # Unknown category: match the free-text name field instead of a tag.
        safe = category.replace('"', "")
        query = (
            f'[out:json][timeout:25];(node["name"~"{safe}",i](around:{radius_m},{lat},{lon});'
            f'way["name"~"{safe}",i](around:{radius_m},{lat},{lon}););out center tags 60;'
        )

    try:
        async with httpx.AsyncClient(timeout=40.0) as client:
            resp = await client.post(
                settings.overpass_url,
                data={"data": query},
                headers={"User-Agent": settings.user_agent},
            )
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
    except httpx.HTTPError as exc:
        return ToolResult.fail(f"OpenStreetMap lookup failed: {exc}")

    places = []
    for element in elements:
        tags_ = element.get("tags", {})
        name = tags_.get("name")
        if not name:
            continue
        centre = element.get("center") or element
        plat, plon = centre.get("lat"), centre.get("lon")
        if plat is None or plon is None:
            continue
        distance = _haversine_m(lat, lon, plat, plon)
        places.append(
            {
                "name": name,
                "distance_m": round(distance),
                "distance_km": round(distance / 1000, 2),
                "address": _address(tags_),
                "phone": tags_.get("phone") or tags_.get("contact:phone", ""),
                "website": tags_.get("website") or tags_.get("contact:website", ""),
                "opening_hours": _format_hours(tags_),
                "lat": plat,
                "lon": plon,
                "maps_url": f"https://maps.apple.com/?ll={plat},{plon}&q={name.replace(' ', '+')}",
            }
        )

    places.sort(key=lambda p: p["distance_m"])
    places = places[: max(1, min(limit, 20))]

    if not places:
        return ToolResult.fail(
            f"Nothing matching '{category}' within {radius_km}km of {origin}. "
            "Try a larger radius or a different term."
        )

    return ToolResult.success(
        {
            "category": category,
            "searched_around": origin,
            "radius_km": radius_km,
            "count": len(places),
            "places": places,
            "note": (
                "OpenStreetMap data may not list phone numbers or hours for every business. "
                "Offer to search the web for a booking page if the user wants to make an appointment."
            ),
        },
        display={"type": "places", "places": places},
    )


async def _geocode(query: str) -> dict | None:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.get(
                f"{settings.nominatim_url}/search",
                params={"q": query, "format": "json", "limit": 1},
                headers={"User-Agent": settings.user_agent},
            )
            resp.raise_for_status()
            rows = resp.json()
    except httpx.HTTPError as exc:
        log.warning("Geocode failed for %r: %s", query, exc)
        return None
    if not rows:
        return None
    row = rows[0]
    return {"lat": float(row["lat"]), "lon": float(row["lon"]), "name": row.get("display_name", query)}


@registry.tool(
    name="where_am_i",
    description=(
        "Resolve the user's current device coordinates into a human-readable place "
        "(neighbourhood, city, country). Use when you need to know where they are for "
        "context — weather, timezone, 'what's around here'."
    ),
    parameters={"type": "object", "properties": {}},
    needs_location=True,
    tags=["location"],
)
async def where_am_i(ctx: ToolContext = None):
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.get(
                f"{settings.nominatim_url}/reverse",
                params={"lat": ctx.lat, "lon": ctx.lon, "format": "json", "zoom": 16},
                headers={"User-Agent": settings.user_agent},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        return ToolResult.fail(f"Reverse geocode failed: {exc}")

    addr = data.get("address", {})
    return ToolResult.success(
        {
            "lat": ctx.lat,
            "lon": ctx.lon,
            "display_name": data.get("display_name", ""),
            "city": addr.get("city") or addr.get("town") or addr.get("village", ""),
            "neighbourhood": addr.get("neighbourhood") or addr.get("suburb", ""),
            "state": addr.get("state", ""),
            "country": addr.get("country", ""),
            "postcode": addr.get("postcode", ""),
        }
    )
