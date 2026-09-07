"""DataUpdateCoordinator: fetch Open-Meteo + recorder history and build the forecast.

Runs the model on a timer, holding the assembled points and the derived summary.
One combined Open-Meteo fetch per refresh (60 past days for the learning, 7 future
days for the forecast), regardless of how many panel orientations are configured;
the model splits that single weather series across orientations itself. The learned
residual map is built from the recorder's own production / SoC history.
"""

from __future__ import annotations

import asyncio
import math
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Dict, List, Optional, Set

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.models import StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .config import (
    battery_from_config,
    inverter_max_w_from_config,
    layout_from_config,
    learning_from_config,
    location_from_config,
    trend_anchor_hour_from_config,
    curtailment_entity_from_config,
    lines_from_config,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BENCHMARK_ENABLED,
    CONF_BENCHMARK_KEY,
    CONF_BENCHMARK_URL,
    CONF_INVERTER_MAX_KW,
)
from .benchmark import DEFAULT_ENDPOINT, async_upload, build_payload
from .checkup import (
    EntitySnapshot,
    Problem,
    check_benchmark_quality,
    check_config,
    check_consumption_coverage,
    check_entities,
    check_production_history,
)
from . import repairs
from .archive import metadata as archive_metadata
from .analog import build_library, enrich_archive_points, enrich_points
from .curtailment import flag_curtailed, on_intervals_from_states
from .battery import BatterySocPoint, project_battery_soc
from .consumption import ConsumptionProfile, build_consumption_profile, consumption_sources
from .trend import TodayTrend, TrendReference, compute_trend, should_capture
from .const import DOMAIN
from .forecast import ForecastPoint, build_forecast_series
from .openmeteo import WeatherSeries, fetch_weather
from .reliability import Reliability, compute_reliability
from .statistics import (
    ARCHIVED_SERIES,
    FORECAST_ENERGY_KEY,
    FORECAST_POWER_KEY,
    WEATHER_FIELDS,
    external_statistic_id,
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

# The interface has no entity to borrow a name from for these series, so the metadata carries one.
_SERIES_NAMES: Dict[str, str] = {key: name for key, _unit, name in ARCHIVED_SERIES}

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=30)
STEP_MINUTES = 15
FORECAST_DAYS = 7
# How far ahead the battery SoC projection runs. Reaches past the FOLLOWING day's solar peak, so a
# chart reading the projection past that point sees it recover rather than reading as stuck at the
# reserve floor. The PV forecast itself already reaches FORECAST_DAYS ahead, so this only uses
# points already being fetched, nothing new to source.
BATTERY_SOC_HORIZON_HOURS = 48.0


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
    # charting.
    weather_forecast: Dict[str, List[dict]]
    # Forecast reliability index (0..100) and its components, feeds the
    # reliability sensor.
    reliability: Reliability
    # Today's outlook versus its frozen daily reference (default 06:00), feeds
    # the today-trend sensor.
    trend: TodayTrend
    # Projected battery state of charge over the next BATTERY_SOC_HORIZON_HOURS (15-min points), from the PV forecast
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
        # Today's already-elapsed live points with the archive's analog clamp applied, for the card's series:
        # the stretch between the archive's last hour and now, which the live series deliberately leaves raw.
        self.elapsed_points: List[ForecastPoint] = []
        # The UTC hour the archive was last recomputed. The 60-day past curve only changes at its
        # trailing hour, so it is rebuilt once an hour rather than on every 30-minute refresh.
        self._last_archive_hour: Optional[datetime] = None
        # The UTC hour up to which the weather statistics have been written. A refresh then imports only
        # the new hours; the full 60-day backfill (self-heal) runs once at startup.
        self._last_weather_stat_hour: Optional[datetime] = None
        # Home consumption profile for the SoC projection; rebuilt hourly by _consumption_profile_for.
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
        # The hour whose prediction was last handed to the benchmark collector, and this integration's
        # version, read once from the manifest so every emission says which model produced it.
        self._last_upload_hour: Optional[datetime] = None
        self._version: Optional[str] = None
        # The check-up (checkup.py): the problems found by the latest pass, the repair issues published for
        # them, whether the production history could be read this refresh (a fetch that failed is not a
        # silent meter), and the collector's latest verdict on this installation.
        self.problems: List[Problem] = []
        self._issue_ids: Set[str] = set()
        self._production_history_read = False
        self._benchmark_quality: Optional[Dict[str, Any]] = None

    def _config(self) -> Dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def consumption_coverage(self) -> Dict[str, float]:
        """Per-source coverage of the consumption profile in use, empty when there is none."""
        return dict(self._consumption_profile.coverage) if self._consumption_profile is not None else {}

    # --- check-up -------------------------------------------------------------------------------

    def _snapshot(self, entity_id: Optional[str]) -> Optional[EntitySnapshot]:
        """What the check-up needs to know of an entity right now."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return EntitySnapshot(entity_id, exists=False)
        attrs = state.attributes
        return EntitySnapshot(
            entity_id,
            exists=True,
            state=state.state,
            unit=attrs.get("unit_of_measurement"),
            device_class=attrs.get("device_class"),
            state_class=attrs.get("state_class"),
        )

    def _check_configuration(self, data: Dict[str, Any]) -> List[Problem]:
        """The configuration and the entities it names. Entities are only judged once Home Assistant is
        running: during startup they routinely do not exist yet, and a false alarm that clears itself
        half an hour later is worse than none."""
        problems = check_config(data, self.hass.config.latitude, self.hass.config.longitude)
        if self.hass.state is CoreState.running:
            problems += check_entities(
                data,
                self._snapshot(learning_from_config(data)),
                self._snapshot(data.get(CONF_BATTERY_SOC_ENTITY) or None),
                self._snapshot(curtailment_entity_from_config(data)),
            )
        return problems

    @callback
    def _publish_problems(self, problems: List[Problem]) -> None:
        self.problems = list(problems)
        self._issue_ids = repairs.sync(self.hass, self.entry, self.problems, self._issue_ids)

    @callback
    def clear_problems(self) -> None:
        repairs.clear(self.hass, self.entry)
        self._issue_ids = set()
        self.problems = []

    async def _async_update_data(self) -> ForecastData:
        data = self._config()
        lat, lon = location_from_config(data, self.hass.config.latitude, self.hass.config.longitude)
        layout = layout_from_config(data)
        cap = inverter_max_w_from_config(data)
        session = async_get_clientsession(self.hass)

        # The configuration is judged before anything is fetched, so a wrong field shows up even when the
        # weather service is down; the data checks join the list as the refresh reads each source.
        problems = self._check_configuration(data)
        self._publish_problems(problems)

        # One combined window: 60 past days feed the learning, 7 future the forecast.
        try:
            weather = await fetch_weather(session, lat, lon, past_days=LEARN_DAYS, forecast_days=FORECAST_DAYS)
            # A transient empty response should not blank the forecast: reuse the last good
            # fetch so the model still runs. The data is only ~30 min old and the
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
        production_entity = learning_from_config(data)
        if production_entity and self._production_history_read:
            problems += check_production_history(
                self._production_buckets, production_entity, lat, lon, layout.total_kwp, now, LEARN_DAYS
            )

        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=FORECAST_DAYS)
        # The pipeline is CPU-bound pure Python (sub-hourly walk, analog scan, 60-day archive); each heavy
        # stage runs in the executor so a refresh never blocks the event loop.
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
            build_library, self._production_buckets, weather, lat, lon, layout, cap
        )
        points = await self.hass.async_add_executor_job(enrich_points, points, analog_library, weather, lat, lon, now)
        # The live series keeps its elapsed points raw on purpose (what the forecast said at the time), but the
        # card draws them next to the archive's clamped hours, where a raw point hugs the nameplate ceiling for
        # up to an hour before the archive catches up. Serve the card a clamped copy of that stretch instead.
        self.elapsed_points = await self.hass.async_add_executor_job(
            enrich_archive_points, [p for p in points if p.t < now], analog_library, weather, lat, lon
        )

        summary = await self.hass.async_add_executor_job(
            partial(summarize, points, now=now, tz=dt_util.DEFAULT_TIME_ZONE, step_minutes=STEP_MINUTES)
        )

        now_utc = dt_util.utcnow()
        self.weather_series = weather
        self.write_weather_statistics(now_utc)
        observed = observed_snapshot(weather, now_utc)
        # Forward-looking weather per field (from today's local midnight), for the sensors'
        # `forecast` chart attribute. Shares the `start` used by the power forecast.
        weather_forecast = weather_forecast_series(weather, start, dt_util.DEFAULT_TIME_ZONE)

        # Archive the predicted production over the past 60-day window at an hourly step, for HA's
        # long-term statistics and the card's past curve. The past only changes at its trailing hour, so
        # rebuild it at most once an hour rather than on every 30-minute refresh (a big CPU saving).
        archive_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        if self._last_archive_hour != archive_hour:
            self.archive_points = await self.hass.async_add_executor_job(
                self._compute_archive_points, now_utc, weather, layout, lat, lon, cap, residual_map, analog_library
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
        if self._consumption_profile is not None:
            problems += check_consumption_coverage(self._consumption_profile.coverage)
        problems += check_benchmark_quality(self._benchmark_quality)
        self._publish_problems(problems)
        await self._maybe_upload_benchmark(data, lat, lon, points, reliability, now_utc)

        return ForecastData(
            points=points,
            summary=summary,
            observed=observed,
            weather_forecast=weather_forecast,
            reliability=reliability,
            trend=trend,
            battery_soc=battery_soc,
        )

    async def _maybe_upload_benchmark(self, data, lat, lon, points, reliability, now_utc) -> None:
        """Hand this hour's prediction to the benchmark collector, when the entry opted in.

        Started beside the refresh instead of inside it: a collector that is slow, unreachable or
        gone must never hold up a forecast. Once an hour, whatever the refresh rate. The whole
        thing is wrapped: an optional upload has no business failing a forecast, so anything that
        goes wrong on the way out is a debug line and nothing more (see benchmark.py).
        """
        if not data.get(CONF_BENCHMARK_ENABLED):
            return
        key = str(data.get(CONF_BENCHMARK_KEY) or "").strip()
        if not key:
            return
        hour = now_utc.replace(minute=0, second=0, microsecond=0)
        if self._last_upload_hour == hour:
            return
        self._last_upload_hour = hour
        try:
            await self._upload_benchmark(data, lat, lon, points, reliability, now_utc, key)
        except Exception as err:  # noqa: BLE001 - see the docstring
            _LOGGER.debug("Benchmark emission skipped: %s", err)

    async def _upload_benchmark(self, data, lat, lon, points, reliability, now_utc, key) -> None:
        """Assemble this hour's emission and hand it to a background task."""
        if self._version is None:
            integration = await async_get_integration(self.hass, DOMAIN)
            self._version = str(integration.version)
        payload = build_payload(
            entry_id=self.entry.entry_id,
            version=self._version,
            emitted_at=now_utc,
            latitude=lat,
            longitude=lon,
            lines=lines_from_config(data),
            country=self.hass.config.country,
            inverter_max_kw=data.get(CONF_INVERTER_MAX_KW),
            points=points,
            reliability=reliability,
            production=self._production_buckets,
            has_battery=bool(data.get(CONF_BATTERY_CAPACITY_KWH) and data.get(CONF_BATTERY_SOC_ENTITY)),
            has_curtailment_signal=bool(curtailment_entity_from_config(data)),
        )
        url = str(data.get(CONF_BENCHMARK_URL) or "").strip() or DEFAULT_ENDPOINT
        session = async_get_clientsession(self.hass)
        self.entry.async_create_background_task(
            self.hass, self._upload_and_note(session, url, key, payload), name=f"{DOMAIN}-benchmark-upload"
        )

    async def _upload_and_note(self, session, url, key, payload) -> None:
        """Send the emission and keep what the collector said of this installation: an exclusion from the
        public figures is a configuration problem the owner should hear about from here, not from the site."""
        answer = await async_upload(session, url, key, payload)
        if not isinstance(answer, dict) or "quality" not in answer:
            return
        quality = answer.get("quality") if isinstance(answer.get("quality"), dict) else {}
        if quality == self._benchmark_quality:
            return
        self._benchmark_quality = quality
        kept = [p for p in self.problems if p.key != "benchmark_excluded"]
        self._publish_problems(kept + check_benchmark_quality(quality))

    async def _project_battery_soc(self, data, points, now) -> List[BatterySocPoint]:
        """Project the battery SoC over the next BATTERY_SOC_HORIZON_HOURS, or [] when the feature can't run.

        Needs three things: the battery config (capacity + live SoC entity), a current SoC reading to
        start from, and a consumption profile derived from the Energy dashboard. Any missing piece
        leaves the projection off rather than guessing.
        """
        battery = battery_from_config(data)
        if battery is None:
            return self._battery_off(
                "no battery is configured in the integration options (set the battery capacity and the "
                "live SoC entity to enable it)",
                not_applicable=True,
            )

        soc_state = self.hass.states.get(battery.soc_entity)
        if soc_state is None or soc_state.state in ("unknown", "unavailable"):
            # Transient at startup or while the battery integration warms up. A state listener re-projects
            # the moment the entity comes back (see async_setup_entry), so this is logged gently rather
            # than as a misconfiguration.
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
                horizon_hours=BATTERY_SOC_HORIZON_HOURS,
                step_minutes=STEP_MINUTES,
            )
        )

    def _battery_off(
        self, reason: str, *, transient: bool = False, not_applicable: bool = False
    ) -> List[BatterySocPoint]:
        """Empty SoC projection, logging the reason once per distinct reason (re-armed on recovery) so a
        steady 'off' does not spam the log. ``transient`` (SoC entity briefly unavailable; the state
        listener retries) and ``not_applicable`` (no battery configured) log at INFO; anything else is an
        actionable misconfiguration and logs at WARNING."""
        if self._battery_off_logged != reason:
            if transient:
                _LOGGER.info("Helios battery SoC projection is off (transient, will retry): %s", reason)
            elif not_applicable:
                _LOGGER.info("Helios battery SoC projection is off: %s", reason)
            else:
                _LOGGER.warning("Helios battery SoC projection is off: %s", reason)
            self._battery_off_logged = reason
        return []

    async def _consumption_profile_for(self, now) -> Optional[ConsumptionProfile]:
        """Home consumption profile from the Energy dashboard, rebuilt at most once an hour.

        The multi-sensor 60-day recorder fetch is the expensive part, so it runs on the archive's hourly
        cadence rather than on every 30-minute refresh; a 60-day average is stable within the hour, and
        the cached profile is reused in between. The hour marker is set as soon as a build is attempted,
        whether or not it yields a profile, so an unconfigured or history-less Energy dashboard is not
        re-queried every 30 minutes either. A transient Energy-dashboard problem keeps the last-good
        profile instead of dropping the projection. Consumption is signed so the ids sum to it: solar +
        grid import - export + battery discharge - charge.
        """
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        if self._last_consumption_hour == this_hour:
            return self._consumption_profile
        self._last_consumption_hour = this_hour

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
        buckets_by_id: Dict[str, List[ProductionBucket]] = {}
        for stat_id, result in zip(ids, fetched):
            if isinstance(result, BaseException):
                _LOGGER.warning(
                    "Helios battery projection: recorder fetch failed for %s, that source is skipped: %s",
                    stat_id,
                    result,
                )
                continue
            buckets_by_id[stat_id] = result

        profile = build_consumption_profile(sources, buckets_by_id, dt_util.DEFAULT_TIME_ZONE)
        if profile is not None:
            self._consumption_profile = profile
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
        """Copy the past weather hours into this integration's own long-term statistics.

        A refresh imports only the hours added since the last write; the full 60-day backfill (install
        and self-heal after downtime) runs once at startup with ``full=True``. Only completed hours are
        written, the in-progress current hour is left out. The series belong to this integration
        rather than to the weather sensors (see archive.py), so nothing here depends on an entity
        being registered and the recorder never writes the same rows from its side.
        """
        weather = self.weather_series
        if weather is None:
            return

        cutoff = now.replace(minute=0, second=0, microsecond=0)
        since = None if (full or self._last_weather_stat_hour is None) else self._last_weather_stat_hour
        wrote_any = False
        for field in WEATHER_FIELDS:
            rows = hourly_statistics(weather.times, getattr(weather, field.attr), cutoff, since=since)
            if not rows:
                continue
            metadata: StatisticMetaData = archive_metadata(
                external_statistic_id(self.entry.entry_id, field.key), field.unit, _SERIES_NAMES[field.key]
            )
            async_add_external_statistics(self.hass, metadata, rows)
            wrote_any = True
        # Advance the marker only once something was actually written, so a call made before the
        # weather window covers a full hour does not skip the backlog the next one still has to cover.
        if wrote_any:
            self._last_weather_stat_hour = cutoff

    def _compute_archive_points(self, now, weather, layout, lat, lon, cap, residual_map, analog_library):
        """Hourly predicted points over the past window [now - LEARN_DAYS, current hour).

        Runs the live model across the past at an hourly step (the cadence HA statistics keep),
        residual-corrected and analog-enriched like the live future points, so the archived curve carries
        the same learned ceiling as the live one. Feeds the statistics backfill and the detail websocket's
        past curve.
        """
        cutoff = now.replace(minute=0, second=0, microsecond=0)
        arch_start = cutoff - timedelta(days=LEARN_DAYS)
        points = build_forecast_series(
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
        return enrich_archive_points(points, analog_library, weather, lat, lon)

    @callback
    def write_forecast_statistics(self) -> None:
        """Copy the predicted-production rows into this integration's own long-term statistics.

        Idempotent: re-importing the trailing window every refresh backfills on install and
        self-heals downtime gaps. Like the weather archive these are integration-owned series, with
        no entity behind them, which is why they survived the removal of the two archive sensors.
        """
        rows_by_key = self._forecast_stat_rows
        if not rows_by_key:
            return

        for key, unit in ((FORECAST_POWER_KEY, "W"), (FORECAST_ENERGY_KEY, "kWh")):
            rows = rows_by_key.get(key)
            if not rows:
                continue
            metadata: StatisticMetaData = archive_metadata(
                external_statistic_id(self.entry.entry_id, key), unit, _SERIES_NAMES[key]
            )
            async_add_external_statistics(self.hass, metadata, rows)

    async def _build_residual_map(self, data, lat, lon, layout, weather, now):
        """Learn the actual/model residual from the recorder's production history."""
        self._production_buckets = []
        self._production_history_read = False
        production_entity = learning_from_config(data)
        if not production_entity:
            return None

        learn_start = now - timedelta(days=LEARN_DAYS)
        try:
            production = await self._fetch_change_buckets(production_entity, learn_start, now)
        except Exception as err:  # noqa: BLE001 - learning is best-effort, forecast still renders
            _LOGGER.warning("Helios learning history fetch failed, forecast stays uncorrected: %s", err)
            return None
        self._production_history_read = True

        # Mark the hours the inverter was held back before anything learns from them (curtailment.py).
        production = await self._flag_curtailed(data, production, learn_start, now)
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

    async def _flag_curtailed(self, data, production, start, end) -> List[ProductionBucket]:
        """Flag the curtailed hours from what the config makes visible: the battery's hourly maximum state of
        charge against the inverter cap, and the optional curtailment entity's on periods. Best-effort: a
        history that cannot be read leaves the buckets unflagged rather than failing the learning."""
        if not production:
            return production
        soc_max_by_start_ms = None
        soc_entity = data.get(CONF_BATTERY_SOC_ENTITY) or None
        cap = inverter_max_w_from_config(data)
        if soc_entity and math.isfinite(cap):
            try:
                rows = await self._statistics(soc_entity, start, end, {"max"}, None)
                soc_max_by_start_ms = {r["start"] * 1000.0: float(r["max"]) for r in rows if r.get("max") is not None}
            except Exception as err:  # noqa: BLE001 - best-effort
                _LOGGER.debug("Battery state-of-charge history unavailable for curtailment detection: %s", err)
        on_intervals = None
        curtail_entity = curtailment_entity_from_config(data)
        if curtail_entity:
            try:
                changes = await get_instance(self.hass).async_add_executor_job(
                    partial(
                        history.state_changes_during_period,
                        self.hass,
                        start,
                        end,
                        curtail_entity,
                        no_attributes=True,
                        include_start_time_state=True,
                    )
                )
                states = [(st.last_updated.timestamp() * 1000.0, st.state) for st in changes.get(curtail_entity, [])]
                on_intervals = on_intervals_from_states(states, start.timestamp() * 1000.0, end.timestamp() * 1000.0)
            except Exception as err:  # noqa: BLE001 - best-effort
                _LOGGER.debug("Curtailment entity history unavailable: %s", err)
        if soc_max_by_start_ms is None and not on_intervals:
            return production
        return flag_curtailed(production, soc_max_by_start_ms=soc_max_by_start_ms, cap_w=cap, on_intervals=on_intervals)

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
