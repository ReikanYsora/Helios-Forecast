"""Opt-in upload of what this installation predicted, for the public accuracy benchmark.

A forecast can only be judged against what actually happened, and a prediction nobody
recorded at the moment it was made cannot be reconstructed afterwards: no provider serves
its own past emissions. So the only way to ever prove a model is right is to write down,
hour after hour, what it announced before reality had a say. That is all this does. Once an
hour it posts the curve this entry currently predicts, together with the production already
measured, and an external collector scores the two against each other once the day is over.

Off unless switched on. What it sends is fixed and deliberately small: the geometry of the
installation, the predicted curve, the measured production and the reliability index. No
entity names, no consumption, no other sensor, nothing about the rest of the house.
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
# goes up when a field changes meaning.
SCHEMA_VERSION = 1

# Where an upload goes when the entry does not name its own collector.
DEFAULT_ENDPOINT = "https://helios-ha.org/bench/v1/emissions"

# One emission an hour. The forecast refreshes twice as often, but the weather behind it does
# not, and the extra origins would only add rows the scoring cannot use.
UPLOAD_INTERVAL = timedelta(hours=1)

# How much measured production travels with each emission. Well past the interval on purpose:
# every upload repeats the recent past, so a collector that missed an hour heals on the next.
OBSERVED_HOURS = 72

# About a kilometre. Sun geometry over that distance is identical and every weather model used
# here has a coarser grid, so the rounding costs the benchmark nothing.
COORD_DECIMALS = 2

_TIMEOUT_S = 15


def site_id(entry_id: str) -> str:
    """Opaque, stable identity of one installation.

    Derived from the config entry rather than stored, so it survives restarts without a file
    and cannot be traced back to the installation it names.
    """
    return hashlib.sha256(f"helios-forecast:{entry_id}".encode()).hexdigest()[:16]


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def build_payload(
    *,
    entry_id: str,
    version: str,
    emitted_at: datetime,
    latitude: float,
    longitude: float,
    lines: List[Dict[str, Any]],
    inverter_max_kw: Optional[float],
    points: List[Any],
    reliability: Any,
    production: List[Any],
    has_battery: bool,
    has_curtailment_signal: bool,
) -> Dict[str, Any]:
    """The complete body of one emission.

    Everything that leaves the installation is assembled here and nowhere else, so what is
    published can be read in one place rather than pieced together from call sites.
    """
    horizon_start = emitted_at - timedelta(hours=OBSERVED_HOURS)
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
            }
            for p in points
        ],
        "observed": observed,
    }


async def async_upload(session: Any, url: str, key: str, payload: Dict[str, Any]) -> bool:
    """Post one emission. True when the collector accepted it.

    Swallows everything: an upload is never a reason for a forecast to fail, and the caller
    has nothing useful to do with the error beyond leaving it in the debug log.
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
                    return False
                return True
    except Exception as err:  # noqa: BLE001 - best effort by design, see the module docstring
        _LOGGER.debug("Benchmark upload failed: %s", err)
        return False
