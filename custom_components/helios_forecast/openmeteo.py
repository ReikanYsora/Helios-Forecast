"""Open-Meteo client.

Thin transport, matching the card's own weather request so the forecast is fed
the same data on ``/v1/forecast``. Hourly cloud layers
(``cloud_cover_low/mid/high``, fused into one weighted cover),
``shortwave_radiation_instant``, ``direct_radiation_instant``,
``diffuse_radiation_instant``, ``temperature_2m``, ``wind_speed_10m``,
``snow_depth``. Values are the per-hour median across
``pick_models_for_location`` (a regional high-resolution model paired with a
global one), the same selection the card uses. A second, best-effort call over a
wider model ensemble adds only the cross-model cloud spread, a
forecast-uncertainty signal.

No unit parameters are sent, so Open-Meteo returns its defaults (W/m2 for
irradiance, degC, snow depth in metres). URL construction and parsing are pure
functions, testable without a network or aiohttp; the async fetchers take a
caller-provided session (HA's shared aiohttp client).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, List, Optional, TypeVar

if TYPE_CHECKING:
    from aiohttp import ClientSession

_BASE_URL = "https://api.open-meteo.com/v1/forecast"

_T = TypeVar("_T")

# Open-Meteo occasionally answers a valid request with a non-200 or an empty payload (a brief
# rate-limit or hiccup). A couple of short retries turn that transient blank into a success instead
# of a failed 30-minute refresh (issue #19).
_RETRY_ATTEMPTS = 3
_RETRY_DELAY_S = 2.0
# Per-request timeout. Without it a stalled connection could hang a refresh indefinitely and leave a
# config entry's sensors unavailable until a manual update_entity; a timeout instead becomes a None
# result that the retry + last-good-reuse path recovers from.
_REQUEST_TIMEOUT_S = 30

WEATHER_HOURLY = (
    "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
    "shortwave_radiation_instant,direct_radiation_instant,diffuse_radiation_instant,"
    "temperature_2m,wind_speed_10m,snow_depth"
)
# The ensemble spread call needs only the aggregate cover to measure model disagreement.
WEATHER_SPREAD_HOURLY = "cloud_cover"

# Wider ensemble, used only for the cross-model cloud spread (a forecast-uncertainty signal). The
# weather VALUES come from pick_models_for_location; this call runs alongside and we read the per-hour
# disagreement across these models. Open-Meteo suffixes each variable key with the model name. A broad,
# global-ish set: models that do not cover the location return nulls and drop out of the spread.
WEATHER_MODELS = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
    "meteofrance_seamless",
)


# Regional high-resolution model (paired with a global one for the median) and its coverage box, per
# area, mirroring the card's picker at 'high' precision (the card's only mode). (model, lat_min,
# lat_max, lon_min, lon_max).
_REGIONAL_MODELS: tuple[tuple[str, float, float, float, float], ...] = (
    ("meteofrance_seamless", 41.3, 51.2, -5.5, 8.5),  # France + Corsica (AROME 1.3 km)
    ("ukmo_seamless", 49.5, 61.0, -10.5, 2.0),  # UK & Ireland (UKMO 2 km)
    ("dwd_icon_seamless", 46.0, 56.0, 5.0, 22.0),  # Central Europe (ICON-D2 2 km)
    ("italia_meteo_arpae_icon_2i", 36.5, 47.0, 10.0, 18.5),  # Italy
    ("metno_seamless", 54.5, 71.5, 4.0, 32.0),  # Nordics (MET Nordic 1 km)
    ("gfs_seamless", 24.5, 49.5, -125.0, -66.5),  # CONUS (HRRR via gfs_seamless)
    ("kma_seamless", 33.0, 39.0, 124.5, 132.0),  # Korea (box enclosed by Japan)
    ("jma_seamless", 24.0, 46.0, 122.0, 146.0),  # Japan (JMA MSM 5 km)
    ("bom_access_global", -47.5, -10.0, 112.0, 179.0),  # Australia & NZ (BOM ACCESS-G)
)


# The coverage boxes overlap (national borders are fuzzy: eastern France also falls in the Central-Europe
# box, southern England in the France box, Korea entirely inside Japan). Of every box the point falls in,
# keep the one it sits most centrally inside, measured as a fraction of each box's own size. That single
# rule resolves both a partial border overlap and a fully enclosed box (Korea's small box outscores the
# vast Japan box at the same point), with no reliance on declaration order. Anywhere uncovered falls back
# to two independent global models, whose median beats either alone.
def pick_models_for_location(lat: float, lon: float) -> list[str]:
    """Model set for the weather request, matching the card's picker."""
    GLOBAL = "ecmwf_ifs025"
    best: str | None = None
    best_score = float("-inf")
    for model, lat_min, lat_max, lon_min, lon_max in _REGIONAL_MODELS:
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            continue
        # Distance to the nearest edge as a fraction of the box's extent (0 on an edge, 0.5 dead centre).
        score = min(
            (lat - lat_min) / (lat_max - lat_min),
            (lat_max - lat) / (lat_max - lat_min),
            (lon - lon_min) / (lon_max - lon_min),
            (lon_max - lon) / (lon_max - lon_min),
        )
        if score > best_score:
            best_score = score
            best = model
    if best is not None:
        return [best, GLOBAL]
    return [GLOBAL, "gfs_seamless"]  # elsewhere: two globals


@dataclass(frozen=True)
class WeatherSeries:
    """Parallel hourly arrays, times are UTC-aware datetimes. Values are the
    per-hour median across pick_models_for_location, matching the card."""

    times: list[datetime]
    cloud: list[float | None]  # weighted cover % (low + 0.6*mid + 0.2*high), 0 where a layer was missing
    shortwave: list[float]  # GHI, W/m2 (instant)
    direct: list[float]  # W/m2 on the horizontal
    diffuse: list[float]  # W/m2 on the horizontal
    temp: list[float]  # degC
    wind: list[float]  # Open-Meteo default (km/h), passed through as the card does
    snow: list[float]  # snow depth, metres
    # Per-hour standard deviation of cloud cover across the ensemble models (model disagreement),
    # overlaid from the best-effort ensemble call. Empty when that call yielded nothing. Read as a
    # forecast-uncertainty signal by the reliability index.
    cloud_spread: list[float] = field(default_factory=list)


def build_weather_url(
    lat: float, lon: float, *, past_days: int = 0, forecast_days: int = 7, ensemble: bool = False
) -> str:
    """URL for the weather (forecast inputs) request.

    Default (``ensemble=False``) fetches the full variable set over ``pick_models_for_location``
    (median-fused), matching the card. ``ensemble=True`` fetches only the aggregate cover over the
    wider model set, used to derive the cross-model cloud spread.
    """
    if ensemble:
        models = ",".join(WEATHER_MODELS)
        hourly = WEATHER_SPREAD_HOURLY
    else:
        models = ",".join(pick_models_for_location(lat, lon))
        hourly = WEATHER_HOURLY
    return (
        f"{_BASE_URL}"
        f"?latitude={lat:.4f}"
        f"&longitude={lon:.4f}"
        f"&hourly={hourly}"
        f"&models={models}"
        f"&past_days={past_days}&forecast_days={forecast_days}&timezone=UTC"
    )


def parse_times(time_strs: list[str]) -> list[datetime]:
    """Open-Meteo ``timezone=UTC`` stamps ('YYYY-MM-DDTHH:MM') to UTC datetimes.

    Mirrors the card appending 'Z' before parsing: the stamps are wall-clock UTC.
    """
    return [datetime.fromisoformat(s).replace(tzinfo=timezone.utc) for s in time_strs]


def _model_arrays(hourly: dict[str, Any], base: str) -> List[list]:
    """Every per-model array for ``base``: the bare key (single-model response) and any
    ``base_<model>`` variants (multi-model response)."""
    out: List[list] = []
    prefix = base + "_"
    for key, val in hourly.items():
        if (key == base or key.startswith(prefix)) and isinstance(val, list):
            out.append(val)
    return out


def _finite_at(arrays: List[list], i: int) -> List[float]:
    vals: List[float] = []
    for a in arrays:
        if i < len(a):
            v = a[i]
            if isinstance(v, (int, float)) and math.isfinite(v):
                vals.append(float(v))
    return vals


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _stdev(vals: List[float]) -> Optional[float]:
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def _clamp_pct(v: Optional[float]) -> float:
    """A cloud-layer percentage clamped to [0, 100]; missing / non-finite reads as 0 (clear)."""
    if v is None or not math.isfinite(v):
        return 0.0
    return max(0.0, min(100.0, v))


def cloud_effective(low: Optional[float], mid: Optional[float], high: Optional[float]) -> float:
    """One weighted cover from the three layers, matching the card: low cloud attenuates far more than
    high cirrus. Each layer is clamped to [0, 100] first; missing layers count as clear."""
    return min(100.0, _clamp_pct(low) + 0.6 * _clamp_pct(mid) + 0.2 * _clamp_pct(high))


def parse_weather(payload: dict[str, Any]) -> WeatherSeries | None:
    """Build a WeatherSeries from an Open-Meteo payload, fusing the picker models to the per-hour
    median and the three cloud layers into one weighted cover. Returns None when there are no
    timestamps or no cloud layers. Works for a single-model response too (median of one = the value)."""
    hourly = payload.get("hourly") or {}
    time_strs = hourly.get("time") or []
    low_arrays = _model_arrays(hourly, "cloud_cover_low")
    mid_arrays = _model_arrays(hourly, "cloud_cover_mid")
    high_arrays = _model_arrays(hourly, "cloud_cover_high")
    if not time_strs or not any(low_arrays):
        return None
    n = len(time_strs)

    def fuse(base: str) -> list:
        arrays = _model_arrays(hourly, base)
        return [_median(_finite_at(arrays, i)) for i in range(n)]

    # Median each layer across models first, then combine into the weighted cover the card uses.
    # cloud_effective always returns a float, but the field is list[float | None] like the fused layers.
    cloud: list[float | None] = [
        cloud_effective(
            _median(_finite_at(low_arrays, i)),
            _median(_finite_at(mid_arrays, i)),
            _median(_finite_at(high_arrays, i)),
        )
        for i in range(n)
    ]

    return WeatherSeries(
        times=parse_times(time_strs),
        cloud=cloud,
        shortwave=fuse("shortwave_radiation_instant"),
        direct=fuse("direct_radiation_instant"),
        diffuse=fuse("diffuse_radiation_instant"),
        temp=fuse("temperature_2m"),
        wind=fuse("wind_speed_10m"),
        snow=fuse("snow_depth"),
        # Baseline zero spread aligned to times; the ensemble call overlays the real cross-model spread.
        cloud_spread=[0.0] * n,
    )


def parse_cloud_spread(payload: dict[str, Any]) -> tuple[list[datetime], list[float]] | None:
    """(times, per-hour cloud spread) from an ENSEMBLE payload, or None when unusable.

    The spread is the standard deviation of cloud cover across the models at each hour. Returned
    with its own times so it can be overlaid onto the values series by timestamp, even if the
    two responses ever cover slightly different hours.
    """
    hourly = payload.get("hourly") or {}
    time_strs = hourly.get("time") or []
    cloud_arrays = _model_arrays(hourly, "cloud_cover")
    if not time_strs or not any(cloud_arrays):
        return None
    spread = [_stdev(_finite_at(cloud_arrays, i)) or 0.0 for i in range(len(time_strs))]
    return parse_times(time_strs), spread


def _overlay_cloud_spread(
    series: WeatherSeries, spread_times: list[datetime], spread_vals: list[float]
) -> WeatherSeries:
    """Return ``series`` with the ensemble cloud spread aligned onto its own hours (0.0 where absent)."""
    by_time = dict(zip(spread_times, spread_vals))
    return replace(series, cloud_spread=[by_time.get(t, 0.0) for t in series.times])


async def _get_json(session: ClientSession, url: str) -> Optional[dict]:
    """GET ``url`` as JSON once. None on a non-200 or a timeout, so the caller's retry +
    last-good-reuse path recovers instead of a stalled request hanging the whole refresh. Other
    transport errors propagate and become an UpdateFailed, retried on the next cycle."""
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT_S):
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except TimeoutError:
        return None


async def _fetch_parsed(
    session: ClientSession, url: str, parser: Callable[[dict[str, Any]], Optional[_T]]
) -> Optional[_T]:
    """GET ``url`` and run ``parser``, retrying an empty/non-200 response (issue #19). None once exhausted."""
    for attempt in range(_RETRY_ATTEMPTS):
        payload = await _get_json(session, url)
        result = parser(payload) if payload is not None else None
        if result is not None:
            return result
        if attempt + 1 < _RETRY_ATTEMPTS:
            await asyncio.sleep(_RETRY_DELAY_S)
    return None


async def fetch_weather(
    session: ClientSession,
    lat: float,
    lon: float,
    *,
    past_days: int = 0,
    forecast_days: int = 7,
) -> WeatherSeries | None:
    """GET the weather inputs. The picker-median call carries the values (required); a second,
    best-effort ensemble call adds only the cross-model cloud spread. If the ensemble call fails, the
    values series is returned with a zero spread rather than failing the whole refresh."""
    base_url = build_weather_url(lat, lon, past_days=past_days, forecast_days=forecast_days)
    series = await _fetch_parsed(session, base_url, parse_weather)
    if series is None:
        return None
    ensemble_url = build_weather_url(lat, lon, past_days=past_days, forecast_days=forecast_days, ensemble=True)
    spread = await _fetch_parsed(session, ensemble_url, parse_cloud_spread)
    if spread is not None:
        series = _overlay_cloud_spread(series, spread[0], spread[1])
    return series
