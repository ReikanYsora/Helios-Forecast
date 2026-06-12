"""DataUpdateCoordinator: fetch Open-Meteo + recorder history and build the forecast.

Runs the model on a timer, holding the assembled points and the derived summary.
One combined Open-Meteo fetch (60 past days for the learning, 7 future days for
the forecast) per weather + per distinct fixed orientation; the learned residual
map is built from the recorder's own production / SoC history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .config import (
    inverter_max_w_from_config,
    layout_from_config,
    learning_from_config,
    location_from_config,
    trend_anchor_hour_from_config,
)
from .analog import build_library, enrich_points
from .trend import TodayTrend, TrendReference, compute_trend, should_capture
from .const import DOMAIN
from .forecast import ForecastPoint, build_forecast_series
from .openmeteo import GtiSeries, WeatherSeries, fetch_gti, fetch_weather
from .reliability import Reliability, compute_reliability
from .solar.gti import orientation_key
from .statistics import (
    FORECAST_ENERGY_KEY,
    FORECAST_POWER_KEY,
    WEATHER_FIELDS,
    forecast_statistics,
    hourly_statistics,
    observed_snapshot,
)
from .solar.residual import (
    LEARN_DAYS,
    ProductionBucket,
    SkyResidualInput,
    SocBucket,
    build_sky_residual_map,
)
from .summary import ForecastSummary, summarize

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=30)
STEP_MINUTES = 15
FORECAST_DAYS = 7


@dataclass
class ForecastData:
    """One refresh worth of output."""

    points: List[ForecastPoint]
    summary: ForecastSummary
    # Current-hour observed weather, keyed by WEATHER_FIELDS key, feeds the
    # weather sensor entities.
    observed: Dict[str, Optional[float]]
    # Forecast reliability index (0..100) and its components, feeds the
    # reliability sensor.
    reliability: Reliability
    # Today's outlook versus its frozen daily reference (default 06:00), feeds
    # the today-trend sensor.
    trend: TodayTrend


class HeliosForecastCoordinator(DataUpdateCoordinator[ForecastData]):
    """Fetches Open-Meteo + recorder history and assembles the PV forecast."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.entry = entry
        # Last weather window fetched, kept so the statistics archive can be
        # written both at the end of a refresh and once right after the sensor
        # entities are registered (so the first backfill lands immediately).
        self.weather_series: Optional[WeatherSeries] = None
        # Predicted-production statistic rows from the most recent refresh, keyed by archive entity
        # key. Written to HA statistics by write_forecast_statistics, both at refresh end and once
        # right after the entities register (first backfill).
        self._forecast_stat_rows: Dict[str, List[Dict[str, Any]]] = {}
        # Hourly predicted points over the past window (now - LEARN_DAYS .. current hour), kept so
        # the detail websocket can serve the past forecast curve the live `points` (today onward) do
        # not cover.
        self.archive_points: List[ForecastPoint] = []
        # Production history (recorder change buckets) from the most recent refresh, kept so the
        # reliability index can reuse it without a second recorder fetch.
        self._production_buckets: List[ProductionBucket] = []
        # Persisted today-trend reference (frozen daily snapshot of the predicted total). Survives
        # restarts so the morning anchor is not lost when HA restarts mid-day.
        self._trend_store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.trend")
        self._trend_ref: Optional[TrendReference] = None
        self._trend_loaded = False

    def _config(self) -> Dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    async def _async_update_data(self) -> ForecastData:
        data = self._config()
        lat, lon = location_from_config(data, self.hass.config.latitude, self.hass.config.longitude)
        layout = layout_from_config(data)
        cap = inverter_max_w_from_config(data)
        session = async_get_clientsession(self.hass)

        # One combined window: 60 past days feed the learning, 7 future the forecast.
        try:
            weather = await fetch_weather(session, lat, lon, past_days=LEARN_DAYS, forecast_days=FORECAST_DAYS)
            store: Dict[str, GtiSeries] = {}
            seen: Set[str] = set()
            for orientation in layout.orientations:
                if orientation.tracker:
                    continue
                key = orientation_key(orientation.tilt_deg, orientation.azimuth_deg)
                if key in seen:
                    continue
                seen.add(key)
                gti = await fetch_gti(
                    session, lat, lon, orientation.tilt_deg, orientation.azimuth_deg,
                    past_days=LEARN_DAYS, forecast_days=FORECAST_DAYS,
                )
                if gti is not None:
                    store[key] = gti
        except Exception as err:  # noqa: BLE001 - any transport error becomes a retry
            raise UpdateFailed(f"Open-Meteo fetch failed: {err}") from err

        if weather is None:
            raise UpdateFailed("Open-Meteo returned no weather data")

        now = dt_util.now()  # local-aware, drives the local-day boundaries
        residual_map = await self._build_residual_map(data, lat, lon, layout, weather, store or None, now)

        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=FORECAST_DAYS)
        points = build_forecast_series(
            weather, store or None, layout, lat, lon,
            inverter_max_w=cap, start=start, end=end, step_minutes=STEP_MINUTES,
            residual_map=residual_map,
        )
        # Analog-ensemble refinement: blend the median of past actual production under similar
        # conditions into the future points and attach the P10/P90 uncertainty band. Reuses the same
        # production history fetched for the residual map.
        analog_library = build_library(self._production_buckets, weather, lat, lon)
        points = enrich_points(points, analog_library, weather, lat, lon, now)

        summary = summarize(points, now=now, tz=dt_util.DEFAULT_TIME_ZONE, step_minutes=STEP_MINUTES)

        now_utc = dt_util.utcnow()
        self.weather_series = weather
        self.write_weather_statistics(now_utc)
        observed = observed_snapshot(weather, now_utc)

        # Archive the predicted production: run the model over the past weather window (the same
        # 60-day window already fetched for learning) at an hourly step, and store it in HA's
        # long-term statistics. Open-Meteo caps the past at LEARN_DAYS, but the statistics are never
        # purged, so the history grows without bound from install onward.
        self.archive_points = self._compute_archive_points(
            now_utc, weather, store or None, layout, lat, lon, cap, residual_map
        )
        self._forecast_stat_rows = forecast_statistics(self.archive_points)
        self.write_forecast_statistics()

        # Reliability index: blends learning maturity, recent predicted-vs-actual skill and today's
        # cloud predictability. Reuses the production history already fetched for the residual map and
        # the hourly archive points just computed, so no extra recorder or model work.
        reliability = compute_reliability(
            self._production_buckets, self.archive_points, weather, now, dt_util.DEFAULT_TIME_ZONE
        )

        trend = await self._today_trend(data, now, summary)

        return ForecastData(
            points=points, summary=summary, observed=observed, reliability=reliability, trend=trend
        )

    async def _today_trend(self, data, now, summary) -> TodayTrend:
        """Today's predicted total versus its frozen daily reference (default 06:00).

        The reference is captured once per day at the first refresh at/after the anchor hour and
        persisted, so it survives restarts; the trend is the current total minus that reference."""
        today_date = now.date().isoformat()
        current = summary.days[0].energy_kwh if summary.days else 0.0

        if not self._trend_loaded:
            stored = await self._trend_store.async_load()
            if stored and stored.get("date") and stored.get("captured_at"):
                self._trend_ref = TrendReference(
                    date=stored["date"],
                    kwh=float(stored["kwh"]),
                    captured_at=dt_util.parse_datetime(stored["captured_at"]),
                )
            self._trend_loaded = True

        anchor = trend_anchor_hour_from_config(data)
        if should_capture(self._trend_ref, today_date, now, anchor):
            self._trend_ref = TrendReference(date=today_date, kwh=current, captured_at=dt_util.utcnow())
            await self._trend_store.async_save(
                {
                    "date": today_date,
                    "kwh": current,
                    "captured_at": self._trend_ref.captured_at.isoformat(),
                }
            )

        return compute_trend(self._trend_ref, current, today_date)

    @callback
    def write_weather_statistics(self, now: datetime) -> None:
        """Copy the past weather hours into HA long-term statistics.

        Idempotent: re-importing the trailing 60-day window every refresh both
        backfills history on install and self-heals any gap left by downtime.
        Only completed hours are written, the in-progress current hour is left
        to the recorder. Skips a field until its sensor entity is registered
        (the statistic_id is the entity_id), so the first call lands once setup
        has added the entities.
        """
        weather = self.weather_series
        if weather is None:
            return

        cutoff = now.replace(minute=0, second=0, microsecond=0)
        registry = er.async_get(self.hass)
        for field in WEATHER_FIELDS:
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{self.entry.entry_id}_{field.key}"
            )
            if entity_id is None:
                continue
            rows = hourly_statistics(weather.times, getattr(weather, field.attr), cutoff)
            if not rows:
                continue
            metadata: StatisticMetaData = {
                "has_mean": True,
                "has_sum": False,
                "name": None,
                "source": "recorder",
                "statistic_id": entity_id,
                "unit_of_measurement": field.unit,
            }
            async_import_statistics(self.hass, metadata, rows)

    def _compute_archive_points(self, now, weather, store, layout, lat, lon, cap, residual_map):
        """Hourly predicted points over the past window [now - LEARN_DAYS, current hour).

        Runs the same model used for the live forecast across the past at an hourly step (the cadence
        HA statistics keep), residual-corrected. Feeds both the statistics backfill and the detail
        websocket's past curve.
        """
        cutoff = now.replace(minute=0, second=0, microsecond=0)
        arch_start = cutoff - timedelta(days=LEARN_DAYS)
        return build_forecast_series(
            weather, store, layout, lat, lon,
            inverter_max_w=cap, start=arch_start, end=cutoff, step_minutes=60,
            residual_map=residual_map,
        )

    @callback
    def write_forecast_statistics(self) -> None:
        """Copy the predicted-production rows into HA long-term statistics.

        Idempotent: re-importing the trailing window every refresh backfills on install and
        self-heals downtime gaps. Skips an archive entity until it is registered (the statistic_id is
        the entity_id), so the first call lands once setup has added the entities.
        """
        rows_by_key = self._forecast_stat_rows
        if not rows_by_key:
            return

        registry = er.async_get(self.hass)
        for key, unit in ((FORECAST_POWER_KEY, "W"), (FORECAST_ENERGY_KEY, "kWh")):
            rows = rows_by_key.get(key)
            if not rows:
                continue
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{self.entry.entry_id}_{key}")
            if entity_id is None:
                continue
            metadata: StatisticMetaData = {
                "has_mean": True,
                "has_sum": False,
                "name": None,
                "source": "recorder",
                "statistic_id": entity_id,
                "unit_of_measurement": unit,
            }
            async_import_statistics(self.hass, metadata, rows)

    async def _build_residual_map(self, data, lat, lon, layout, weather, store, now):
        """Learn the actual/model residual from the recorder's production history."""
        self._production_buckets = []
        production_entity, soc_entity, cutoff = learning_from_config(data)
        if not production_entity:
            return None

        learn_start = now - timedelta(days=LEARN_DAYS)
        try:
            production = await self._fetch_change_buckets(production_entity, learn_start, now)
            soc_series = (
                await self._fetch_mean_buckets(soc_entity, learn_start, now)
                if (soc_entity and cutoff is not None)
                else None
            )
        except Exception as err:  # noqa: BLE001 - learning is best-effort, forecast still renders
            _LOGGER.warning("Helios learning history fetch failed, forecast stays uncorrected: %s", err)
            return None

        self._production_buckets = production
        if not production:
            return None

        return build_sky_residual_map(
            SkyResidualInput(
                lat=lat, lon=lon, layout=layout, production=production,
                cloud_times=[t.timestamp() * 1000.0 for t in weather.times],
                cloud=weather.cloud, shortwave=weather.shortwave, direct=weather.direct,
                diffuse=weather.diffuse, temp=weather.temp, wind=weather.wind, snow=weather.snow,
                gti_store=store, soc_series=soc_series, cutoff_soc=cutoff,
                now_ms=now.timestamp() * 1000.0,
            )
        )

    async def _statistics(self, stat_id, start, end, types, units):
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, end, {stat_id}, "hour", units, types
        )
        return result.get(stat_id, [])

    async def _fetch_change_buckets(self, stat_id, start, end) -> List[ProductionBucket]:
        rows = await self._statistics(stat_id, start, end, {"change"}, {"energy": "kWh"})
        return [
            ProductionBucket(start_ms=r["start"] * 1000.0, end_ms=r["end"] * 1000.0, kwh=r["change"])
            for r in rows
            if r.get("change") is not None
        ]

    async def _fetch_mean_buckets(self, stat_id, start, end) -> List[SocBucket]:
        rows = await self._statistics(stat_id, start, end, {"mean"}, None)
        return [
            SocBucket(start_ms=r["start"] * 1000.0, end_ms=r["end"] * 1000.0, mean=r["mean"])
            for r in rows
            if r.get("mean") is not None
        ]
