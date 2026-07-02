"""Tiny geo helpers. No external deps — keeps the project on free tiers."""
import math


def haversine_km(lat1, lon1, lat2, lon2) -> float | None:
    """Great-circle distance in km between two lat/lng points.
    Returns None if either point is missing coordinates."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0  # earth radius km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def maps_directions_url(dest_lat, dest_lng, dest_label: str | None = None) -> str:
    """Google Maps directions URL (deep link) — opens in Maps from chat."""
    if dest_lat is None or dest_lng is None:
        return f"https://www.google.com/maps/search/?api=1&query={dest_label or ''}".replace(" ", "+")
    return f"https://www.google.com/maps/dir/?api=1&destination={dest_lat},{dest_lng}"
