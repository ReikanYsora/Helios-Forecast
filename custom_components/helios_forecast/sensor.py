"""Sensor entities, the integration's first-class automation surface.

Thin readers over the coordinator's summary: power now / next hour, per-day peak
power + peak time and daily energy over the 7-day horizon, plus today-remaining
and this/next hour. All values are produced in summary.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Union

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfIrradiance,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ForecastData, HeliosForecastCoordinator
from .statistics import WEATHER_FIELDS
from .summary import ForecastSummary

_HORIZON_DAYS = 7
_ValueType = Optional[Union[float, datetime]]


@dataclass(frozen=True, kw_only=True)
class HeliosSensorDescription(SensorEntityDescription):
    """A sensor description that knows how to read its value from the summary."""

    value_fn: Callable[[ForecastSummary], _ValueType]
    # Optional extra attributes derived from the full forecast (e.g. the dense
    # curve for charting), kept off the recorder via _unrecorded_attributes.
    attrs_fn: Optional[Callable[[ForecastData], dict]] = None


def _forecast_attrs(data: ForecastData) -> dict:
    """The dense forecast curve as a chart-friendly attribute (W per bucket)."""
    return {"forecast": [{"datetime": p.t.isoformat(), "watts": round(p.pv_w, 2)} for p in data.points]}


def _power(
    key: str,
    name: str,
    value_fn: Callable[[ForecastSummary], _ValueType],
    attrs_fn: Optional[Callable[[ForecastData], dict]] = None,
) -> HeliosSensorDescription:
    return HeliosSensorDescription(
        key=key,
        name=name,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=value_fn,
        attrs_fn=attrs_fn,
    )


def _energy(key: str, name: str, value_fn: Callable[[ForecastSummary], _ValueType]) -> HeliosSensorDescription:
    # No state_class: these are forecast values, not a metered total. The ENERGY device
    # class forbids `measurement` (HA rejects energy + measurement), and `total` /
    # `total_increasing` would wrongly imply an accumulating meter. The long-term
    # statistics the card reads are written directly by the coordinator, not derived
    # from these sensors' state_class.
    return HeliosSensorDescription(
        key=key,
        name=name,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=value_fn,
    )


def _archive_energy(key: str, name: str, value_fn: Callable[[ForecastSummary], _ValueType]) -> HeliosSensorDescription:
    # Archive entity: its purpose is the long-term mean statistics the coordinator imports
    # (the card's past predicted-production curve). Those imported stats are entity-bound
    # (statistic_id == entity_id), so the entity MUST carry a state_class, otherwise HA flags
    # "entity no longer has a state class" on every statistics cycle. kWh + MEASUREMENT is valid
    # only WITHOUT the energy device class (HA rejects energy + measurement), so we drop
    # device_class here, the same pattern the trend sensor uses.
    return HeliosSensorDescription(
        key=key,
        name=name,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=value_fn,
    )


def _timestamp(key: str, name: str, value_fn: Callable[[ForecastSummary], _ValueType]) -> HeliosSensorDescription:
    return HeliosSensorDescription(
        key=key,
        name=name,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=value_fn,
    )


def _build_descriptions() -> list[HeliosSensorDescription]:
    descriptions: list[HeliosSensorDescription] = [
        _power("power_now", "Power now", lambda s: s.power_now_w, attrs_fn=_forecast_attrs),
        # Analog uncertainty band on the current power: P10 (low) and P90 (high). None until the
        # analog support is solid enough to surface a band.
        _power("power_now_low", "Power now (low)", lambda s: s.power_now_low_w),
        _power("power_now_high", "Power now (high)", lambda s: s.power_now_high_w),
        _power("power_next_hour", "Power next hour", lambda s: s.power_next_hour_w),
        _energy("energy_today_remaining", "Energy today remaining", lambda s: s.energy_today_remaining_kwh),
        _energy("energy_this_hour", "Energy this hour", lambda s: s.energy_this_hour_kwh),
        _energy("energy_next_hour", "Energy next hour", lambda s: s.energy_next_hour_kwh),
        # Archive entities: their live value mirrors the prediction for the current hour, but their
        # purpose is the long-term statistics the coordinator backfills (predicted production history,
        # kept by HA well beyond Open-Meteo's 60-day window so the card can draw the past forecast).
        _power("predicted_power", "Predicted power", lambda s: s.power_now_w),
        _archive_energy("predicted_energy", "Predicted energy", lambda s: s.energy_this_hour_kwh),
    ]
    for n in range(1, _HORIZON_DAYS + 1):
        i = n - 1
        # `i=i` binds the loop index into each lambda; mypy can't infer the default-arg lambda's type.
        descriptions.append(_energy(f"energy_day_{n}", f"Energy day {n}", lambda s, i=i: s.days[i].energy_kwh))  # type: ignore[misc]
        descriptions.append(_power(f"peak_power_day_{n}", f"Peak power day {n}", lambda s, i=i: s.days[i].peak_power_w))  # type: ignore[misc]
        descriptions.append(_timestamp(f"peak_time_day_{n}", f"Peak time day {n}", lambda s, i=i: s.days[i].peak_time))  # type: ignore[misc]
    return descriptions


# Display metadata for the archived weather sensors, keyed by WEATHER_FIELDS key.
# (device_class, unit, name, suggested_display_precision). The unit must equal
# the field's WEATHER_FIELDS.unit string so the entity and its statistics agree.
_WEATHER_META: dict[str, tuple[Optional[SensorDeviceClass], str, str, int]] = {
    "cloud_cover": (None, PERCENTAGE, "Cloud cover", 0),
    "ghi": (SensorDeviceClass.IRRADIANCE, UnitOfIrradiance.WATTS_PER_SQUARE_METER, "Global irradiance", 0),
    "direct": (SensorDeviceClass.IRRADIANCE, UnitOfIrradiance.WATTS_PER_SQUARE_METER, "Direct irradiance", 0),
    "diffuse": (SensorDeviceClass.IRRADIANCE, UnitOfIrradiance.WATTS_PER_SQUARE_METER, "Diffuse irradiance", 0),
    "temperature": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, "Temperature", 1),
    "wind_speed": (SensorDeviceClass.WIND_SPEED, UnitOfSpeed.KILOMETERS_PER_HOUR, "Wind speed", 1),
    "snow_depth": (SensorDeviceClass.DISTANCE, UnitOfLength.METERS, "Snow depth", 2),
}


def _build_weather_descriptions() -> list[SensorEntityDescription]:
    """One MEASUREMENT sensor per archived Open-Meteo weather variable."""
    descriptions: list[SensorEntityDescription] = []
    for field in WEATHER_FIELDS:
        device_class, unit, name, precision = _WEATHER_META[field.key]
        descriptions.append(
            SensorEntityDescription(
                key=field.key,
                name=name,
                device_class=device_class,
                native_unit_of_measurement=unit,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=precision,
            )
        )
    return descriptions


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Helios",
        model="Forecast",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor entities for a config entry."""
    coordinator: HeliosForecastCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        HeliosForecastSensor(coordinator, entry, description) for description in _build_descriptions()
    ]
    entities += [HeliosWeatherSensor(coordinator, entry, description) for description in _build_weather_descriptions()]
    entities.append(HeliosReliabilitySensor(coordinator, entry))
    entities.append(HeliosTodayTrendSensor(coordinator, entry))
    async_add_entities(entities)


class HeliosForecastSensor(CoordinatorEntity[HeliosForecastCoordinator], SensorEntity):
    """A single forecast value, read from the coordinator's summary."""

    entity_description: HeliosSensorDescription
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self,
        coordinator: HeliosForecastCoordinator,
        entry: ConfigEntry,
        description: HeliosSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> _ValueType:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data.summary)

    @property
    def extra_state_attributes(self) -> Optional[dict]:
        if self.entity_description.attrs_fn is None or self.coordinator.data is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class HeliosWeatherSensor(CoordinatorEntity[HeliosForecastCoordinator], SensorEntity):
    """An archived Open-Meteo weather value, read from the current-hour snapshot.

    The long-term history of these entities is written to HA statistics by the
    coordinator (see write_weather_statistics), which is what keeps it available
    beyond Open-Meteo's rolling 60-day window.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HeliosForecastCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Optional[float]:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.observed.get(self.entity_description.key)


class HeliosReliabilitySensor(CoordinatorEntity[HeliosForecastCoordinator], SensorEntity):
    """Forecast reliability index (0..100), blending learning maturity, recent
    predicted-vs-actual skill and today's cloud predictability. The components and
    a per-horizon-day breakdown ride along as attributes."""

    _attr_has_entity_name = True
    _attr_name = "Forecast reliability"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:shield-check"
    # The per-day list is a chart-style attribute, kept off the recorder.
    _unrecorded_attributes = frozenset({"per_day"})

    def __init__(self, coordinator: HeliosForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_reliability"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Optional[float]:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.reliability.overall

    @property
    def extra_state_attributes(self) -> Optional[dict]:
        if self.coordinator.data is None:
            return None
        r = self.coordinator.data.reliability
        return {
            "data_maturity": r.data_maturity,
            "recent_skill": r.recent_skill,
            "today_predictability": r.today_predictability,
            "days_learned": r.days_learned,
            "per_day": r.per_day,
        }


class HeliosTodayTrendSensor(CoordinatorEntity[HeliosForecastCoordinator], SensorEntity):
    """How much today's predicted total has moved since its frozen daily reference
    (default the 06:00 snapshot). Signed kWh: positive when the day now looks better
    than at the reference, negative when worse. Unknown before the reference is set."""

    _attr_has_entity_name = True
    _attr_name = "Today forecast trend"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:trending-up"

    def __init__(self, coordinator: HeliosForecastCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_today_trend"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Optional[float]:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.trend.delta_kwh

    @property
    def extra_state_attributes(self) -> Optional[dict]:
        if self.coordinator.data is None:
            return None
        t = self.coordinator.data.trend
        return {
            "reference_kwh": t.reference_kwh,
            "reference_time": t.reference_time.isoformat() if t.reference_time else None,
            "current_kwh": round(t.current_kwh, 2),
            "direction": t.direction,
        }
