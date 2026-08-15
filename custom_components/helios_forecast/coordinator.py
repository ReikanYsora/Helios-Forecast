"""DataUpdateCoordinator: fetch Open-Meteo + recorder history and build the forecast.

Runs the model on a timer, holding the assembled points and the derived summary.
One combined Open-Meteo fetch (60 past days for the learning, 7 future days for
the forecast) per weather + per distinct fixed orientation; the learned residual
map is built from the recorder's own production / SoC history.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Dict, List, Optional

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
    battery_from_config,
    inverter_max_w_from_config,
    layout_from_config,
    learning_from_config,
    location_from_config,
    trend_anchor_hour_from_config,
)
from .analog import build_library, enrich_points
from .battery import BatterySocPoint, project_battery_soc
from .consumption import ConsumptionProfile, build_consumption_profile, consumption_sources
from .trend import TodayTrend, TrendReference, compute_trend, should_capture
from .const import DOMAIN
from .forecast import ForecastPoint, build_forecast_series
from .openmeteo import WeatherSeries, fetch_weather
from .reliability import Reliability, compute_reliability
from .statistics import (
    FORECAST_ENERGY_KEY,
    FORECAST_POWER_KEY,
    WEATHER_FIELDS,
    forecast_statistics,
    hourly_statistics,
    observed_snapshot,
    weather_forecast_series,
)
from .solar.residual import (
    LEARN_DAYS,
    ProductionBucket,
    SkyResidualInput,
    build_sky_residual_map,
)
from .summary import ForecastSummary, summarize

# HA-version compat, both mandatory in StatisticMetaData from HA 2026.11 and imported defensively so
# the integration still loads on older cores that lack them.
# `mean_type` replaces the deprecated `has_mean`.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_TYPE_ARITHMETIC = StatisticMeanType.ARITHMETIC
except ImportError:  # pragma: no cover - older HA cores
    _MEAN_TYPE_ARITHMETIC = None


# `unit_class` names the unit-conversion class HA uses to migrate a statistic's history if its unit
# later changes. We derive the unit -> class map from the core's own converters so the value always
# matches the installed core. Units with no converter (e.g. W/m2 irradiance) map to None, the correct
# "not convertible" answer. The key is always declared in the metadata; cores predating it ignore it.
def _build_unit_classes() -> Dict[str, Optional[str]]:
    mapping: Dict[str, Optional[str]] = {}
    try:
        from homeassistant.util import unit_conversion as _uc
    except ImportError:  # pragma: no cover - older HA cores
        return mapping
    for name in (
        "PowerConverter",
        "EnergyConverter",
        "TemperatureConverter",
        "SpeedConverter",
        "DistanceConverter",
        "UnitlessRatioConverter",
    ):
        converter = getattr(_uc, name, None)
        unit_class = getattr(converter, "UNIT_CLASS", None)
        if converter is None or unit_class is None:
            continue
        for unit in getattr(converter, "VALID_UNITS", ()):  # e.g. "W" -> "power"
            mapping.setdefault(unit, unit_class)
    return mapping


_UNIT_CLASSES: Dict[str, Optional[str]] = _build_unit_classes()

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
    # Forward-looking hourly series per weather field (today + horizon), keyed by
    # WEATHER_FIELDS key, exposed as each weather sensor's `forecast` attribute for
    # charting (issue #21).
    weather_forecast: Dict[str, List[dict]]
    # Forecast reliability index (0..100) and its components, feeds the
    # reliability sensor.
    reliability: Reliability
    # Today's outlook versus its frozen daily reference (default 06:00), feeds
    # the today-trend sensor.
    trend: TodayTrend
    # Projected battery state of charge over the next 24 h (15-min points), from the PV forecast
    # against the learned consumption profile. Empty when the battery feature is off or has no
    # usable input (no capacity / SoC entity / consumption history). Feeds the SoC sensor + service.
    battery_soc: List[BatterySocPoint]


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
        # The UTC hour the archive was last recomputed. The 60-day past curve only changes at its
        # trailing hour, so it is rebuilt once an hour rather than on every 30-minute refresh.
        self._last_archive_hour: Optional[datetime] = None
        # The UTC hour up to which the weather statistics have been written. A refresh then imports only
        # the new hours; the full 60-day backfill (self-heal) runs once at startup.
        self._last_weather_stat_hour: Optional[datetime] = None
        # Home consumption profile for the battery SoC projection, rebuilt at most once an hour. Its
        # multi-sensor 60-day recorder fetch is the expensive part, so it runs on the same hourly cadence
        # as the prediction archive rather than on every 30-minute refresh (the 2026.8.3 CPU/network
        # lesson); a 60-day average barely moves within an hour. Reused in between.
        self._consumption_profile: Optional[ConsumptionProfile] = None
        self._last_consumption_hour: Optional[datetime] = None
        # Production history (recorder change buckets) from the most recent refresh, kept so the
        # reliability index can reuse it without a second recorder fetch.
        self._production_buckets: List[ProductionBucket] = []
        # Persisted today-trend reference (frozen daily snapshot of the predicted total). Survives
        # restarts so the morning anchor is not lost when HA restarts mid-day.
        self._trend_store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.trend")
        self._trend_ref: Optional[TrendReference] = None
        self._trend_loaded = False
        # Last-logged reason the battery SoC projection was skipped, so _battery_off() warns once per reason.
        self._battery_off_logged: Optional[str] = None

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
            # A transient empty response should not blank the forecast: reuse the last good
            # fetch so the model still runs (issue #19). The data is only ~30 min old and the
            # next refresh recovers; a first-ever empty response (no prior fetch) still fails.
            if weather is None and self.weather_series is not None:
                _LOGGER.warning("Open-Meteo returned no weather data; reusing the last successful fetch")
                weather = self.weather_series
        except Exception as err:  # noqa: BLE001 - any transport error becomes a retry
            raise UpdateFailed(f"Open-Meteo fetch failed: {err}") from err

        if weather is None:
            raise UpdateFailed("Open-Meteo returned no weather data")

        now = dt_util.now()  # local-aware, drives the local-day boundaries
        residual_map = await self._build_residual_map(data, lat, lon, layout, weather, now)

        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=FORECAST_DAYS)
        # The forecast pipeline is CPU-bound pure Python (the sub-hourly walk, the analog scan, the
        # 60-day archive). Each heavy stage runs in the executor so it never blocks the event loop: on a
        # small box a 30-minute refresh used to spike a core to 100% and starve the network for seconds.
        points = await self.hass.async_add_executor_job(
            partial(
                build_forecast_series,
                weather,
                layout,
                lat,
                lon,
                inverter_max_w=cap,
                start=start,
                end=end,
                step_minutes=STEP_MINUTES,
                residual_map=residual_map,
            )
        )
        # Analog-ensemble refinement: blend the median of past actual production under similar
        # conditions into the future points and attach the P10/P90 uncertainty band. Reuses the same
        # production history fetched for the residual map.
        analog_library = await self.hass.async_add_executor_job(
            build_library, self._production_buckets, weather, lat, lon
        )
        points = await self.hass.async_add_executor_job(enrich_points, points, analog_library, weather, lat, lon, now)

        summary = await self.hass.async_add_executor_job(
            partial(summarize, points, now=now, tz=dt_util.DEFAULT_TIME_ZONE, step_minutes=STEP_MINUTES)
        )

        now_utc = dt_util.utcnow()
        self.weather_series = weather
        self.write_weather_statistics(now_utc)
        observed = observed_snapshot(weather, now_utc)
        # Forward-looking weather per field (from today's local midnight), for the sensors'
        # `forecast` chart attribute (issue #21). Shares the `start` used by the power forecast.
        weather_forecast = weather_forecast_series(weather, start, dt_util.DEFAULT_TIME_ZONE)

        # Archive the predicted production over the past 60-day window at an hourly step, for HA's
        # long-term statistics and the card's past curve. The past only changes at its trailing hour, so
        # rebuild it at most once an hour rather than on every 30-minute refresh (a big CPU saving).
        archive_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        if self._last_archive_hour != archive_hour:
            self.archive_points = await self.hass.async_add_executor_job(
                self._compute_archive_points, now_utc, weather, layout, lat, lon, cap, residual_map
            )
            self._forecast_stat_rows = await self.hass.async_add_executor_job(forecast_statistics, self.archive_points)
            self.write_forecast_statistics()
            self._last_archive_hour = archive_hour

        # Reliability index: blends learning maturity, recent predicted-vs-actual skill and today's
        # cloud predictability. Reuses the production history already fetched for the residual map and
        # the hourly archive points, so no extra recorder or model work.
        reliability = await self.hass.async_add_executor_job(
            compute_reliability, self._production_buckets, self.archive_points, weather, now, dt_util.DEFAULT_TIME_ZONE
        )

        trend = await self._today_trend(data, now, summary)
        battery_soc = await self._project_battery_soc(data, points, now)

        return ForecastData(
            points=points,
            summary=summary,
            observed=observed,
            weather_forecast=weather_forecast,
            reliability=reliability,
            trend=trend,
            battery_soc=battery_soc,
        )

    async def _project_battery_soc(self, data, points, now) -> List[BatterySocPoint]:
        """Project the battery SoC over the next 24 h, or [] when the feature can't run.

        Needs three things: the battery config (capacity + live SoC entity), a current SoC reading to
        start from, and a consumption profile derived from the Energy dashboard. Any missing piece
        leaves the projection off rather than guessing.
        """
        battery = battery_from_config(data)
        if battery is None:
            return self._battery_off(
                "no battery is configured in the integration options (set the battery capacity and the "
                "live SoC entity to enable it)"
            )

        soc_state = self.hass.states.get(battery.soc_entity)
        if soc_state is None or soc_state.state in ("unknown", "unavailable"):
            # Transient at startup or while the battery integration warms up (a modbus link can take a
            # few seconds to open). A state listener re-projects the moment the entity comes back (see
            # async_setup_entry), so this is logged gently rather than as a misconfiguration.
            return self._battery_off(f"the SoC entity {battery.soc_entity} is unavailable", transient=True)
        try:
            start_soc_frac = float(soc_state.state) / 100.0
        except (TypeError, ValueError):
            return self._battery_off(
                f"the SoC entity {battery.soc_entity} reads '{soc_state.state}', which is not a 0-100 number"
            )

        profile = await self._consumption_profile_for(now)
        if profile is None:
            return self._battery_off(
                "home consumption is not available yet from the Energy dashboard, so there is nothing to "
                "discharge against"
            )

        self._battery_off_logged = None
        return await self.hass.async_add_executor_job(
            partial(
                project_battery_soc,
                battery,
                start_soc_frac,
                points,
                profile,
                now=now,
                tz=dt_util.DEFAULT_TIME_ZONE,
                step_minutes=STEP_MINUTES,
            )
        )

    def _battery_off(self, reason: str, *, transient: bool = False) -> List[BatterySocPoint]:
        """Return an empty SoC projection, logging why the first time a given reason occurs.

        The projection is deliberately skipped when an input is missing rather than guessed; without
        this the sensor just read ``unknown`` with no hint, which made it impossible to tell a
        misconfiguration from a transient gap. Logged once per distinct reason (re-armed when the
        projection recovers) so a steady-state 'off' does not spam the log every refresh. A
        ``transient`` reason (the SoC entity briefly unavailable at startup, which the state listener
        recovers from within seconds) logs at INFO; an actionable misconfiguration logs at WARNING."""
        if self._battery_off_logged != reason:
            if transient:
                _LOGGER.info("Helios battery SoC projection is off (transient, will retry): %s", reason)
            else:
                _LOGGER.warning("Helios battery SoC projection is off: %s", reason)
            self._battery_off_logged = reason
        return []

    async def _consumption_profile_for(self, now) -> Optional[ConsumptionProfile]:
        """Home consumption profile from the Energy dashboard, rebuilt at most once an hour.

        The multi-sensor 60-day recorder fetch is the expensive part, so it runs on the archive's hourly
        cadence rather than on every 30-minute refresh; a 60-day average is stable within the hour, and
        the cached profile is reused in between. A transient Energy-dashboard problem keeps the last-good
        profile instead of dropping the projection. Consumption is signed so the ids sum to it: solar +
        grid import - export + battery discharge - charge.
        """
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        if self._consumption_profile is not None and self._last_consumption_hour == this_hour:
            return self._consumption_profile

        try:
            from homeassistant.components.energy import async_get_manager

            manager = await async_get_manager(self.hass)
            prefs = manager.data
        except Exception as err:  # noqa: BLE001 - the SoC projection is best-effort, the forecast still renders
            _LOGGER.warning("Helios battery projection: Energy dashboard unavailable, SoC skipped: %s", err)
            return self._consumption_profile

        sources = consumption_sources(prefs)
        if not sources.signed:
            _LOGGER.warning(
                "Helios battery projection: the Energy dashboard has no configured sources, so home "
                "consumption cannot be derived and the SoC projection stays off"
            )
            return self._consumption_profile

        learn_start = now - timedelta(days=LEARN_DAYS)
        ids = list(sources.signed)
        fetched = await asyncio.gather(
            *(self._fetch_change_buckets(stat_id, learn_start, now) for stat_id in ids),
            return_exceptions=True,
        )
        buckets_by_id = {
            stat_id: result for stat_id, result in zip(ids, fetched) if not isinstance(result, BaseException)
        }

        profile = build_consumption_profile(sources, buckets_by_id, dt_util.DEFAULT_TIME_ZONE)
        if profile is not None:
            self._consumption_profile = profile
            self._last_consumption_hour = this_hour
        return self._consumption_profile

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
    def write_weather_statistics(self, now: datetime, *, full: bool = False) -> None:
        """Copy the past weather hours into HA long-term statistics.

        A refresh imports only the hours added since the last write; the full 60-day backfill (install
        and self-heal after downtime) runs once at startup with ``full=True``. Only completed hours are
        written, the in-progress current hour is left to the recorder. Skips a field until its sensor
        entity is registered (the statistic_id is the entity_id), so the first backfill lands once setup
        has added the entities.
        """
        weather = self.weather_series
        if weather is None:
            return

        cutoff = now.replace(minute=0, second=0, microsecond=0)
        since = None if (full or self._last_weather_stat_hour is None) else self._last_weather_stat_hour
        registry = er.async_get(self.hass)
        wrote_any = False
        for field in WEATHER_FIELDS:
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{self.entry.entry_id}_{field.key}")
            if entity_id is None:
                continue
            rows = hourly_statistics(weather.times, getattr(weather, field.attr), cutoff, since=since)
            if not rows:
                continue
            # mean_type + unit_class are declared statically (not added after the literal) so both the
            # runtime and static API scanners see them. has_mean stays for cores older than mean_type
            # (HA 2025.1), where the extra mean_type key is simply ignored and has_mean is read instead.
            metadata: StatisticMetaData = {
                "has_mean": True,
                "mean_type": _MEAN_TYPE_ARITHMETIC,
                "has_sum": False,
                "name": None,
                "source": "recorder",
                "statistic_id": entity_id,
                "unit_of_measurement": field.unit,
                "unit_class": _UNIT_CLASSES.get(field.unit),
            }
            async_import_statistics(self.hass, metadata, rows)
            wrote_any = True
        # Advance the marker only once the entities exist and a write happened, so a pre-registration
        # call does not skip the backlog the first real backfill still has to cover.
        if wrote_any:
            self._last_weather_stat_hour = cutoff

    def _compute_archive_points(self, now, weather, layout, lat, lon, cap, residual_map):
        """Hourly predicted points over the past window [now - LEARN_DAYS, current hour).

        Runs the same model used for the live forecast across the past at an hourly step (the cadence
        HA statistics keep), residual-corrected. Feeds both the statistics backfill and the detail
        websocket's past curve.
        """
        cutoff = now.replace(minute=0, second=0, microsecond=0)
        arch_start = cutoff - timedelta(days=LEARN_DAYS)
        return build_forecast_series(
            weather,
            layout,
            lat,
            lon,
            inverter_max_w=cap,
            start=arch_start,
            end=cutoff,
            step_minutes=60,
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
            # Same static mean_type + unit_class declaration as the weather archive above.
            metadata: StatisticMetaData = {
                "has_mean": True,
                "mean_type": _MEAN_TYPE_ARITHMETIC,
                "has_sum": False,
                "name": None,
                "source": "recorder",
                "statistic_id": entity_id,
                "unit_of_measurement": unit,
                "unit_class": _UNIT_CLASSES.get(unit),
            }
            async_import_statistics(self.hass, metadata, rows)

    async def _build_residual_map(self, data, lat, lon, layout, weather, now):
        """Learn the actual/model residual from the recorder's production history."""
        self._production_buckets = []
        production_entity = learning_from_config(data)
        if not production_entity:
            return None

        learn_start = now - timedelta(days=LEARN_DAYS)
        try:
            production = await self._fetch_change_buckets(production_entity, learn_start, now)
        except Exception as err:  # noqa: BLE001 - learning is best-effort, forecast still renders
            _LOGGER.warning("Helios learning history fetch failed, forecast stays uncorrected: %s", err)
            return None

        self._production_buckets = production
        if not production:
            _LOGGER.warning(
                "Production history for %s is empty: the entity has no long-term sum statistics "
                "(pick a cumulative energy sensor in kWh, not a power sensor); learning is off and "
                "the reliability index stays capped until then",
                production_entity,
            )
            return None

        # The map build is pure CPU (walking the production buckets against the sky grid); run it in the
        # executor so it never blocks the event loop.
        return await self.hass.async_add_executor_job(
            build_sky_residual_map,
            SkyResidualInput(
                lat=lat,
                lon=lon,
                layout=layout,
                production=production,
                cloud_times=[t.timestamp() * 1000.0 for t in weather.times],
                cloud=weather.cloud,
                shortwave=weather.shortwave,
                direct=weather.direct,
                diffuse=weather.diffuse,
                temp=weather.temp,
                wind=weather.wind,
                snow=weather.snow,
                now_ms=now.timestamp() * 1000.0,
            ),
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
