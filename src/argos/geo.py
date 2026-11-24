"""Geographic distance and dynamic value-based radius helpers."""

import math
from typing import Optional

from argos.config import Settings


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance between two coordinates in statute miles."""
    earth_radius_miles = 3958.7613
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(earth_radius_miles * c, 2)


def recommended_radius_miles(estimated_value_usd: Optional[float], settings: Settings) -> int:
    """Scale search radius dynamically from the 15-mile + 5-mile baseline."""
    if estimated_value_usd is None or estimated_value_usd < 200.0:
        return settings.radius_base_miles
    if estimated_value_usd < 500.0:
        return settings.radius_mid_miles
    return settings.radius_high_miles
