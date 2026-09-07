"""Opt-in upload of what this installation predicted, for the public accuracy benchmark.

A forecast can only be judged against what actually happened, and a prediction nobody
recorded at the moment it was made cannot be reconstructed afterwards: no provider serves
its own past emissions. So the only way to ever prove a model is right is to write down,
hour after hour, what it announced before reality had a say. That is all this does. Once an
hour it posts the curve this entry currently predicts, together with the production already
measured, and an external collector scores the two against each other once the day is over.

Off unless switched on. What it sends is fixed and stays about the sky and the roof: the geometry
of the installation, the predicted curve, the weather the model read to produce it, the measured
production, and what the learning stood on. No entity names, no consumption, no other sensor,
nothing about the rest of the house.
Coordinates are rounded to two decimals, roughly a kilometre, which no weather model can
tell apart and which keeps a street address out of the upload. The site is identified by a
hash of the config entry, so the collector can follow one installation over time without
ever being told whose it is.

The upload runs beside the refresh, never inside it: it cannot delay a forecast, and any
failure (server down, no network, bad key) is dropped after a debug line. A missed hour is
a missing row in someone's benchmark, never a broken integration.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

_LOGGER = logging.getLogger(__name__)

# Payload shape. The collector refuses what it does not know how to read, so this only ever
# goes up when a field changes meaning. 2 adds the weather the model actually read, the ground
# elevation, the local zone, the per-point analog confidence and what the learning stood on:
# without those, an error can be measured but not attributed, and a benchmark that cannot say
# whether the sky or the model was wrong cannot make either of them better.
SCHEMA_VERSION = 2

# Where an upload goes when the entry does not name its own collector.
DEFAULT_ENDPOINT = "https://helios-ha.org/bench/v1/emissions"

# What the learning block may say. An allow-list rather than whatever the coordinator hands over,
# because this module's docstring promises that everything leaving an installation is assembled here
# and nowhere else, and a dict copied through unread makes that false: a field added upstream while
# diagnosing something would travel to a public export with nothing here or in the tests to stop it.
LEARNING_FIELDS: tuple[str, ...] = (
    "production_hours",
    "curtailed_hours",
    "analog_samples",
    "sky_cells",
    "sky_cells_total",
    "residual_global",
    "learn_days",
)

# One emission an hour. The forecast refreshes twice as often, but the weather behind it does
# not, and the extra origins would only add rows the scoring cannot use.
UPLOAD_INTERVAL = timedelta(hours=1)

# How much measured production travels with each emission. Well past the interval on purpose:
# every upload repeats the recent past, so a collector that missed an hour heals on the next.
OBSERVED_HOURS = 72

# How far ahead the curve travels at its native sub-hourly step. Past it the forecast is thinned to
# the top of each hour, and nothing is lost by that: an hour is scored against the meter's reading
# for the hour that contains it, so four points inside one hour are four comparisons with the same
# truth. Sub-hourly detail earns its place in the near term, where a battery decision turns on it.
DENSE_HOURS = 24

# About a kilometre. Sun geometry over that distance is identical and every weather model used
# here has a coarser grid, so the rounding costs the benchmark nothing.
COORD_DECIMALS = 2

# The elevation travels in bands, for the same reason the position travels rounded. The weather
# service answers with the height of the exact point it was asked about, to a tenth of a metre, and
# a value that fine undoes the coordinate rounding on its own: intersected with a public terrain
# model it leaves only the few points of the cell that stand at that height, and the address the
# rounding removed comes back. A hundred-metre band still says whether a roof is at sea level or at
# fifteen hundred metres, which is the only thing the field is for.
ELEVATION_STEP_M = 100

_TIMEOUT_S = 15


def site_id(entry_id: str) -> str:
    """Opaque, stable identity of one installation.

    Derived from the config entry rather than stored, so it survives restarts without a file
    and cannot be traced back to the installation it names.
    """
    return hashlib.sha256(f"helios-forecast:{entry_id}".encode()).hexdigest()[:16]


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _banded(value: Any, step: float) -> Optional[float]:
    """`value` snapped to the nearest multiple of `step`, or None. Coarsening, not rounding: the
    point is that the published number says less than the one that was measured."""
    if not isinstance(value, (int, float)):
        return None
    return round(float(value) / step) * step


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def thin_forecast(points: List[Any], emitted_at: datetime) -> List[Any]:
    """The curve as it travels: every point up to DENSE_HOURS ahead, the top of each hour beyond.

    A point whose time cannot be read is dropped rather than guessed at, and a curve that is already
    hourly comes back untouched.
    """
    out: List[Any] = []
    dense_until = emitted_at + timedelta(hours=DENSE_HOURS)
    for p in points:
        moment = getattr(p, "t", None)
        if not isinstance(moment, datetime):
            continue
        if moment < dense_until or (moment.minute == 0 and moment.second == 0):
            out.append(p)
    return out


def weather_series(weather: Any, since: datetime) -> List[Dict[str, Any]]:
    """The hourly weather the model read, from `since` onward, as plain rows.

    Every field the physics consumes, and the ensemble's own disagreement beside it. An hour with no
    usable timestamp is dropped rather than sent with a null time; a missing value stays null, since
    "the service had nothing here" is itself worth recording.
    """
    times = list(getattr(weather, "times", None) or [])
    if not times:
        return []
    fields = (
        ("ghi", "shortwave"),
        ("direct", "direct"),
        ("diffuse", "diffuse"),
        ("temp", "temp"),
        ("wind", "wind"),
        ("snow", "snow"),
        ("cloud", "cloud"),
        ("cloud_spread", "cloud_spread"),
    )
    arrays = {name: list(getattr(weather, attr, None) or []) for name, attr in fields}
    rows: List[Dict[str, Any]] = []
    for i, moment in enumerate(times):
        if not isinstance(moment, datetime) or moment < since:
            continue
        row: Dict[str, Any] = {"t": _iso(moment)}
        for name, values in arrays.items():
            value = values[i] if i < len(values) else None
            row[name] = _round(value, 2) if isinstance(value, (int, float)) else None
        rows.append(row)
    return rows


def build_payload(
    *,
    entry_id: str,
    version: str,
    emitted_at: datetime,
    latitude: float,
    longitude: float,
    lines: List[Dict[str, Any]],
    country: Optional[str],
    time_zone: Optional[str],
    inverter_max_kw: Optional[float],
    points: List[Any],
    reliability: Any,
    production: List[Any],
    weather: Any,
    learning: Dict[str, Any],
    has_battery: bool,
    has_curtailment_signal: bool,
) -> Dict[str, Any]:
    """The complete body of one emission.

    Everything that leaves the installation is assembled here and nowhere else, so what is
    published can be read in one place rather than pieced together from call sites.
    """
    horizon_start = emitted_at - timedelta(hours=OBSERVED_HOURS)
    weather_rows = weather_series(weather, horizon_start) if weather is not None else []
    curve = thin_forecast(points, emitted_at)
    observed = [
        {
            "t": _iso(datetime.fromtimestamp(b.start_ms / 1000.0, tz=emitted_at.tzinfo)),
            "kwh": _round(b.kwh, 4),
            "curtailed": bool(getattr(b, "curtailed", False)),
        }
        for b in production
        if b.start_ms >= horizon_start.timestamp() * 1000.0
    ]
    return {
        "schema": SCHEMA_VERSION,
        "site_id": site_id(entry_id),
        "emitted_at": _iso(emitted_at),
        "model_version": version,
        "site": {
            "latitude": round(latitude, COORD_DECIMALS),
            "longitude": round(longitude, COORD_DECIMALS),
            # The country the installation sits in. Far coarser than the coordinates already sent,
            # and what lets the benchmark say which climates it actually covers.
            "country": (country or None),
            # The installation's own zone. A morning bias cannot be compared between installations
            # without it, and deducing one from the longitude gets the boundary cases wrong.
            "time_zone": (time_zone or None),
            # Ground elevation where this installation is, to the nearest ELEVATION_STEP_M.
            "elevation_m": _banded(getattr(weather, "elevation_m", None), ELEVATION_STEP_M),
            "inverter_max_kw": _round(inverter_max_kw, 3),
            "has_battery": has_battery,
            "has_curtailment_signal": has_curtailment_signal,
            "lines": [
                {
                    "azimuth": _round(line.get("azimuth"), 1),
                    "tilt": _round(line.get("tilt"), 1),
                    "kwp": _round(line.get("kwp"), 3),
                    "tracker": line.get("tracker") or None,
                }
                for line in lines
            ],
        },
        "reliability": {
            "overall": _round(getattr(reliability, "overall", None), 1),
            "data_maturity": _round(getattr(reliability, "data_maturity", None), 3),
            "recent_skill": _round(getattr(reliability, "recent_skill", None), 3),
            "days_learned": getattr(reliability, "days_learned", None),
        },
        # The prediction itself: the blended value the sensors publish, the pure physical model
        # beside it so an ablation can be scored without rerunning anything, and the band.
        "forecast": [
            {
                "t": _iso(p.t),
                "w": _round(p.pv_w, 2),
                "raw_w": _round(p.pv_raw_w, 2),
                "p10": _round(p.pv_p10, 2),
                "p90": _round(p.pv_p90, 2),
                "cloud": _round(p.cloud, 1),
                # How much the analog ensemble was trusted here, 0 to 1, or null where it had
                # nothing to say. It separates an error of the learning from an error of the physics.
                "conf": _round(getattr(p, "analog_confidence", None), 3),
            }
            for p in curve
        ],
        # The weather the model read, hourly, over the same window the emission speaks about. Without
        # it a wrong forecast cannot be told from a wrong sky, which is the first question to ask of
        # any of these numbers.
        "weather": weather_rows,
        # What the learning stood on at this moment: how much history it had, how much of it it had
        # to set aside, and how much of the sky it had learned. A score means something different
        # from an installation with sixty days behind it than from one with three. Projected through
        # LEARNING_FIELDS, so what leaves is what this module says leaves.
        "learning": {key: learning.get(key) for key in LEARNING_FIELDS},
        "observed": observed,
    }


async def async_upload(session: Any, url: str, key: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Post one emission. The collector's answer (a dict) when it accepted it, None otherwise.

    The answer carries `quality`, the collector's verdict on this installation (excluded from the
    public figures, and why), which the check-up turns into a repair issue. Swallows everything
    else: an upload is never a reason for a forecast to fail, and the caller has nothing useful to
    do with the error beyond leaving it in the debug log.
    """
    try:
        async with asyncio.timeout(_TIMEOUT_S):
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            ) as response:
                if response.status >= 400:
                    _LOGGER.debug("Benchmark upload refused with status %s", response.status)
                    return None
                try:
                    answer = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 - an empty or odd body is still an accepted upload
                    answer = {}
                return answer if isinstance(answer, dict) else {}
    except Exception as err:  # noqa: BLE001 - best effort by design, see the module docstring
        _LOGGER.debug("Benchmark upload failed: %s", err)
        return None
