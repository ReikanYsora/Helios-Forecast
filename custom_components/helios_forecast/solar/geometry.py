"""Solar position math.

Faithful port of the Helios card's ``getSunPosition``. Pure functions, no Home
Assistant imports. The model is the simplified declination + equation-of-time
formulation the card validated against the NOAA SPA reference (mean altitude
error 0.30 deg, mean azimuth error 0.36 deg over 376 samples across a year and
eight latitudes). It is kept identical here so the server-side forecast matches
what the card produced, which is what lets us prove parity against the
TypeScript output rather than just "close enough".
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

_D = math.pi / 180.0


class SunPosition(NamedTuple):
    """Sun altitude and azimuth in degrees, azimuth clockwise from north."""

    altitude: float
    azimuth: float


def sun_position(moment: datetime, lat: float, lon: float) -> SunPosition:
    """Sun altitude / azimuth at a UTC instant for a lat/lon point.

    ``moment`` is read in UTC; a naive datetime is assumed to already be UTC.
    Both returned values are in degrees, azimuth measured clockwise from north.
    """
    when = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)

    hour = when.hour + when.minute / 60.0 + when.second / 3600.0

    # Day of year, 1-based. Mirrors the card's floor(ms / 86_400_000) over a
    # reference of Dec 31 00:00 UTC of the previous year (JS Date.UTC(year, 0, 0)),
    # so a partial day inside the current date is truncated exactly as it is there.
    ref = datetime(when.year, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    doy = math.floor((when - ref).total_seconds() / 86_400.0)

    decl = 23.45 * math.sin(_D * (360.0 / 365.0) * (doy - 81))
    b = _D * (360.0 / 365.0) * (doy - 81)
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)

    # Hour angle normalised to [-180, 180] so its sign reliably marks AM vs PM
    # at longitudes far from Greenwich (without this, NYC / Tokyo / Sydney land
    # the azimuth up to 180 deg off).
    ha = 15.0 * (hour + lon / 15.0 + eot / 60.0 - 12.0)
    ha = ((ha + 180.0) % 360.0 + 360.0) % 360.0 - 180.0

    sin_a = math.sin(_D * lat) * math.sin(_D * decl) + math.cos(_D * lat) * math.cos(_D * decl) * math.cos(_D * ha)
    alt = math.asin(max(-1.0, min(1.0, sin_a))) / _D
    cos_alt = math.cos(alt * _D)
    cos_az = (
        (math.sin(_D * decl) - math.sin(_D * lat) * sin_a) / (math.cos(_D * lat) * cos_alt) if cos_alt > 1e-4 else 0.0
    )
    az = math.acos(max(-1.0, min(1.0, cos_az))) / _D
    if ha > 0:
        az = 360.0 - az

    return SunPosition(altitude=alt, azimuth=az)
