"""Open-Meteo client.

Thin transport, faithful to the card's own requests so the forecast is fed the
exact same data. Two endpoints on ``/v1/forecast``:

  - weather: hourly ``cloud_cover, shortwave_radiation, direct_radiation,
    diffuse_radiation, temperature_2m, wind_speed_10m, snow_depth``. The values
    come from best_match (the model the Open-Meteo app shows, and the one the GTI
    already uses), so what we display and what we compute agree with the app the
    user checks against. A second, best-effort call over the model ensemble adds
    only the cross-model cloud spread, kept as a forecast-uncertainty signal (#22).
  - GTI: hourly ``global_tilted_irradiance_instant`` for one tilt + azimuth
    (ported from gti.ts). Open-Meteo accepts a single orientation per request,
    so a multi-orientation install needs one GET per distinct orientation.

No unit parameters are sent, exactly like the card, so Open-Meteo returns its
defaults (W/m2 for irradiance, degC, snow depth in metres) and the model
interprets the arrays the same way the card does. URL construction and parsing
are pure functions so they can be tested without a network or aiohttp; the async
fetchers take a caller-provided session (HA's shared aiohttp client).
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
    "cloud_cover,shortwave_radiation,direct_radiation,diffuse_radiation,temperature_2m,wind_speed_10m,snow_depth"
)
GTI_HOURLY = "global_tilted_irradiance_instant"

# Multi-model ensemble, used only for the cross-model cloud spread (a forecast-uncertainty signal).
# The weather VALUES come from best_match; this ensemble call runs alongside it and we read the
# per-hour disagreement across these models. Open-Meteo returns each variable once per model (key
# suffixed with the model name). A broad, global-ish set: models that do not cover the location
# return nulls and simply drop out of the spread.
WEATHER_MODELS = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
    "meteofrance_seamless",
)


@dataclass(frozen=True)
class WeatherSeries:
    """Parallel hourly arrays, times are UTC-aware datetimes. Values come from the
    best_match model (matching the Open-Meteo app and the GTI request)."""

    times: list[datetime]
    cloud: list[float | None]  # %, None where the model was missing that hour
    shortwave: list[float]  # GHI, W/m2
    direct: list[float]  # W/m2 on the horizontal
    diffuse: list[float]  # W/m2 on the horizontal
    temp: list[float]  # degC
    wind: list[float]  # Open-Meteo default (km/h), passed through as the card does
    snow: list[float]  # snow depth, metres
    # Per-hour standard deviation of cloud cover across the ensemble models (model disagreement),
    # overlaid from the best-effort ensemble call. Empty when that call yielded nothing. Read as a
    # forecast-uncertainty signal by the reliability index.
    cloud_spread: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class GtiSeries:
    """Plane-of-array irradiance (W/m2) per hour for one orientation."""

    times: list[datetime]
    poa: list[float]


def om_azimuth(helios_azimuth_deg: float) -> int:
    """Helios azimuth (0 = north) to Open-Meteo azimuth (0 = south), rounded.

    Open-Meteo uses 0 = south, -90 = east, +90 = west, +/-180 = north, so the
    conversion is ``omAz = ourAz - 180``. Not normalised to a range, matching the
    card (Open-Meteo accepts the raw value).
    """
    return round(helios_azimuth_deg - 180)


def build_weather_url(
    lat: float, lon: float, *, past_days: int = 0, forecast_days: int = 7, ensemble: bool = False
) -> str:
    """URL for the weather (forecast inputs) request.

    Default (``ensemble=False``) is a best_match request: no ``models=``, so Open-Meteo returns its
    per-location best model, matching the app and the GTI request. ``ensemble=True`` adds ``models=``
    to fetch the whole set, used only to derive the cross-model cloud spread.
    """
    models = f"&models={','.join(WEATHER_MODELS)}" if ensemble else ""
    return (
        f"{_BASE_URL}"
        f"?latitude={lat:.4f}"
        f"&longitude={lon:.4f}"
        f"&hourly={WEATHER_HOURLY}"
        f"{models}"
        f"&past_days={past_days}&forecast_days={forecast_days}&timezone=UTC"
    )


def build_gti_url(
    lat: float,
    lon: float,
    tilt_deg: float,
    azimuth_deg: float,
    *,
    past_days: int = 0,
    forecast_days: int = 7,
) -> str:
    """URL for one orientation's GTI request. ``azimuth_deg`` is Helios convention."""
    return (
        f"{_BASE_URL}"
        f"?latitude={lat:.4f}"
        f"&longitude={lon:.4f}"
        f"&hourly={GTI_HOURLY}"
        f"&tilt={round(tilt_deg)}"
        f"&azimuth={om_azimuth(azimuth_deg)}"
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


def parse_weather(payload: dict[str, Any]) -> WeatherSeries | None:
    """Build a WeatherSeries from an Open-Meteo payload, fusing the model ensemble
    to the per-hour median. Returns None when there are no timestamps or no cloud
    series. Works for a single-model response too (median of one = the value)."""
    hourly = payload.get("hourly") or {}
    time_strs = hourly.get("time") or []
    cloud_arrays = _model_arrays(hourly, "cloud_cover")
    if not time_strs or not any(cloud_arrays):
        return None
    n = len(time_strs)

    def fuse(base: str) -> list:
        arrays = _model_arrays(hourly, base)
        return [_median(_finite_at(arrays, i)) for i in range(n)]

    cloud = [_median(_finite_at(cloud_arrays, i)) for i in range(n)]
    cloud_spread = [_stdev(_finite_at(cloud_arrays, i)) or 0.0 for i in range(n)]

    return WeatherSeries(
        times=parse_times(time_strs),
        cloud=cloud,
        shortwave=fuse("shortwave_radiation"),
        direct=fuse("direct_radiation"),
        diffuse=fuse("diffuse_radiation"),
        temp=fuse("temperature_2m"),
        wind=fuse("wind_speed_10m"),
        snow=fuse("snow_depth"),
        cloud_spread=cloud_spread,
    )


def parse_cloud_spread(payload: dict[str, Any]) -> tuple[list[datetime], list[float]] | None:
    """(times, per-hour cloud spread) from an ENSEMBLE payload, or None when unusable.

    The spread is the standard deviation of cloud cover across the models at each hour. Returned
    with its own times so it can be overlaid onto the best_match series by timestamp, even if the
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


def parse_gti(payload: dict[str, Any]) -> GtiSeries | None:
    """Build a GtiSeries from an Open-Meteo payload, or None when unusable."""
    hourly = payload.get("hourly") or {}
    time_strs = hourly.get("time") or []
    poa = hourly.get("global_tilted_irradiance_instant") or []
    if not time_strs or not poa:
        return None
    return GtiSeries(times=parse_times(time_strs), poa=poa)


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
    """GET the weather inputs. best_match carries the values (required); a second, best-effort
    ensemble call adds only the cross-model cloud spread (#22). If the ensemble call fails, the
    best_match series is returned with a zero spread rather than failing the whole refresh."""
    base_url = build_weather_url(lat, lon, past_days=past_days, forecast_days=forecast_days)
    series = await _fetch_parsed(session, base_url, parse_weather)
    if series is None:
        return None
    ensemble_url = build_weather_url(lat, lon, past_days=past_days, forecast_days=forecast_days, ensemble=True)
    spread = await _fetch_parsed(session, ensemble_url, parse_cloud_spread)
    if spread is not None:
        series = _overlay_cloud_spread(series, spread[0], spread[1])
    return series


async def fetch_gti(
    session: ClientSession,
    lat: float,
    lon: float,
    tilt_deg: float,
    azimuth_deg: float,
    *,
    past_days: int = 0,
    forecast_days: int = 7,
) -> GtiSeries | None:
    """GET one orientation's GTI, retrying an empty/non-200 response. None once exhausted."""
    url = build_gti_url(lat, lon, tilt_deg, azimuth_deg, past_days=past_days, forecast_days=forecast_days)
    return await _fetch_parsed(session, url, parse_gti)
