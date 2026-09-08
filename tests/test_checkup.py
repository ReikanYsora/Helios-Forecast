"""Tests for the configuration and data check-up: every rule, with the value that trips it."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.checkup import (  # noqa: E402
    ERROR,
    WARNING,
    EntitySnapshot,
    check_config,
    check_consumption_coverage,
    check_entities,
    check_production_history,
)
from custom_components.helios_forecast.solar.residual import ProductionBucket  # noqa: E402

HOME = (44.1, 1.4)
_UTC = timezone.utc


def _keys(problems):
    return sorted(p.issue_id for p in problems)


def _line(**over):
    base = {"azimuth": 180.0, "tilt": 30.0, "kwp": 3.0, "tracker": "none"}
    base.update(over)
    return base


def _config(**over):
    base = {"arrays": [_line()], "production_entity": "sensor.pv_energy"}
    base.update(over)
    return base


# --- configuration ---------------------------------------------------------------------------


def test_a_sound_configuration_has_no_problem() -> None:
    assert check_config(_config(), *HOME) == []


def test_no_line_is_an_error() -> None:
    problems = check_config(_config(arrays=[]), *HOME)
    assert _keys(problems) == ["no_lines"]
    assert problems[0].severity == ERROR


def test_peak_power_in_watts_is_named_with_its_value_and_line() -> None:
    problems = check_config(_config(arrays=[_line(), _line(kwp=4050.0)]), *HOME)
    assert _keys(problems) == ["line_kwp_unit_2"]
    assert problems[0].placeholders == {"line": "2", "kwp": "4050"}


def test_missing_or_zero_peak_power() -> None:
    assert _keys(check_config(_config(arrays=[_line(kwp=0)]), *HOME)) == ["line_kwp_missing_1"]
    assert _keys(check_config(_config(arrays=[_line(kwp=None)]), *HOME)) == ["line_kwp_missing_1"]


def test_tilt_azimuth_and_tracker_bounds() -> None:
    problems = check_config(_config(arrays=[_line(tilt=95.0, azimuth=400.0, tracker="spinning")]), *HOME)
    assert _keys(problems) == ["line_azimuth_1", "line_tilt_1", "line_tracker_1"]
    assert all(p.severity == ERROR for p in problems)


def test_line_far_from_the_home_is_a_warning_with_the_distance() -> None:
    # About 111 km north of the home.
    problems = check_config(_config(arrays=[_line(latitude=45.1, longitude=1.4)]), *HOME)
    assert _keys(problems) == ["line_location_far_1"]
    assert problems[0].severity == WARNING
    assert problems[0].placeholders["km"] == "111"


def test_line_coordinates_out_of_range() -> None:
    problems = check_config(_config(arrays=[_line(latitude=144.1, longitude=1.4)]), *HOME)
    assert _keys(problems) == ["line_location_invalid_1"]


def test_inverter_limit_in_watts_and_limit_too_low() -> None:
    unit = check_config(_config(inverter_max_kw=21000.0, arrays=[_line(kwp=14.4)]), *HOME)
    assert _keys(unit) == ["inverter_cap_unit"]
    assert unit[0].placeholders == {"cap": "21000", "kwp": "14.4"}
    low = check_config(_config(inverter_max_kw=0.5, arrays=[_line(kwp=6.0)]), *HOME)
    assert _keys(low) == ["inverter_cap_low"]
    assert low[0].severity == WARNING
    fine = check_config(_config(inverter_max_kw=5.0, arrays=[_line(kwp=6.0)]), *HOME)
    assert fine == []


def test_line_inverter_limit_in_watts() -> None:
    problems = check_config(_config(arrays=[_line(kwp=3.0, line_inverter_max_kw=3000.0)]), *HOME)
    assert _keys(problems) == ["line_cap_unit_1"]


def test_configured_location_far_from_home_assistant_is_a_warning() -> None:
    problems = check_config(_config(latitude=48.8, longitude=2.3), *HOME)
    assert _keys(problems) == ["location_far"]
    assert int(problems[0].placeholders["km"]) > 400


def test_configured_location_out_of_range_is_an_error() -> None:
    assert _keys(check_config(_config(latitude=91.0, longitude=0.0), *HOME)) == ["location_invalid"]


def test_no_production_entity_is_a_warning_not_an_error() -> None:
    problems = check_config(_config(production_entity=None), *HOME)
    assert _keys(problems) == ["production_entity_unset"]
    assert problems[0].severity == WARNING


def test_trend_anchor_hour_bounds() -> None:
    assert _keys(check_config(_config(trend_anchor_hour=25), *HOME)) == ["trend_anchor_hour"]


def test_battery_block() -> None:
    assert check_config(_config(), *HOME) == []  # no battery at all: nothing to say
    problems = check_config(
        _config(
            battery_capacity_kwh=10.0,
            battery_soc_entity="sensor.soc",
            battery_max_charge_kw=100.0,
            battery_max_discharge_kw=5.0,
            battery_min_soc=150.0,
            battery_efficiency=20.0,
        ),
        *HOME,
    )
    assert _keys(problems) == ["battery_efficiency", "battery_min_soc", "battery_power_charge"]
    half = check_config(_config(battery_capacity_kwh=10.0), *HOME)
    assert _keys(half) == ["battery_soc_entity_unset"]
    other_half = check_config(_config(battery_soc_entity="sensor.soc"), *HOME)
    assert _keys(other_half) == ["battery_capacity_unset"]
    absurd = check_config(_config(battery_capacity_kwh=5000.0, battery_soc_entity="sensor.soc"), *HOME)
    assert _keys(absurd) == ["battery_capacity"]


def test_production_entity_must_exist_and_be_a_cumulative_energy_sensor() -> None:
    data = _config()
    missing = EntitySnapshot("sensor.pv_energy", exists=False)
    assert _keys(check_entities(data, missing, None, None)) == ["production_entity_missing"]
    power = EntitySnapshot("sensor.pv_energy", exists=True, unit="W", device_class="power", state_class="measurement")
    problems = check_entities(data, power, None, None)
    assert _keys(problems) == ["production_entity_kind"]
    assert problems[0].placeholders["unit"] == "W"
    energy = EntitySnapshot(
        "sensor.pv_energy", exists=True, unit="kWh", device_class="energy", state_class="total_increasing"
    )
    assert check_entities(data, energy, None, None) == []


def test_battery_soc_and_curtailment_entities() -> None:
    data = _config(battery_soc_entity="sensor.soc", curtailment_entity="binary_sensor.limit")
    soc = EntitySnapshot("sensor.soc", exists=True, unit="kWh")
    cut = EntitySnapshot("binary_sensor.limit", exists=False)
    assert _keys(check_entities(data, None, soc, cut)) == ["battery_soc_entity_unit", "curtailment_entity_missing"]
    assert (
        check_entities(
            data, None, EntitySnapshot("sensor.soc", True, unit="%"), EntitySnapshot("binary_sensor.limit", True)
        )
        == []
    )


# --- production history ----------------------------------------------------------------------


def _bucket(start: datetime, kwh: float) -> ProductionBucket:
    ms = start.timestamp() * 1000.0
    return ProductionBucket(start_ms=ms, end_ms=ms + 3600_000.0, kwh=kwh)


def _days(now: datetime, days: int, day_kwh: float, night_kwh: float = 0.0):
    """Hourly buckets over `days` days: production 09:00-15:00 UTC, optional night reading at 01:00."""
    out = []
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(days):
        day = start + timedelta(days=d)
        for h in range(24):
            kwh = day_kwh if 9 <= h < 15 else (night_kwh if h == 1 else 0.0)
            out.append(_bucket(day + timedelta(hours=h), kwh))
    return out


NOW = datetime(2026, 6, 20, 12, tzinfo=_UTC)


def test_healthy_history_has_no_problem() -> None:
    assert check_production_history(_days(NOW, 10, 1.0), "sensor.pv", *HOME, 3.0, NOW, 60) == []


def test_empty_history() -> None:
    assert _keys(check_production_history([], "sensor.pv", *HOME, 3.0, NOW, 60)) == ["production_history_empty"]


def test_production_at_night_is_an_error_with_the_energy() -> None:
    problems = check_production_history(_days(NOW, 10, 1.0, night_kwh=0.3), "sensor.pv", *HOME, 3.0, NOW, 60)
    assert _keys(problems) == ["production_at_night"]
    assert problems[0].severity == ERROR
    assert problems[0].placeholders["kwh"] == "3"


def test_a_little_twilight_energy_is_not_night() -> None:
    # 0.01 kWh at 01:00 over 10 days: under the half-kWh floor and under 3 % of the day.
    assert check_production_history(_days(NOW, 10, 1.0, night_kwh=0.01), "sensor.pv", *HOME, 3.0, NOW, 60) == []


def test_production_above_the_declared_panels() -> None:
    problems = check_production_history(_days(NOW, 10, 5.0), "sensor.pv", *HOME, 3.0, NOW, 60)
    assert _keys(problems) == ["production_above_panels"]
    assert problems[0].placeholders == {"entity": "sensor.pv", "kw": "5", "kwp": "3"}


def test_silent_meter_for_days() -> None:
    old = _days(NOW - timedelta(days=5), 10, 1.0)  # last production 5 days ago
    problems = check_production_history(old, "sensor.pv", *HOME, 3.0, NOW, 60)
    assert _keys(problems) == ["production_stale"]
    assert problems[0].placeholders["days"] == "5"
    never = [_bucket(NOW - timedelta(hours=h), 0.0) for h in range(1, 200)]
    assert _keys(check_production_history(never, "sensor.pv", *HOME, 3.0, NOW, 60)) == ["production_stale"]


# --- consumption coverage ---------------------------------------------------------------------


def test_sparse_consumption_source_is_named_with_its_share() -> None:
    problems = check_consumption_coverage({"sensor.import": 0.999, "sensor.bat_out": 0.24, "sensor.export": 0.98})
    assert _keys(problems) == ["consumption_source_sparse_sensor_bat_out"]
    assert problems[0].placeholders == {"source": "sensor.bat_out", "pct": "24", "best": "100"}
    assert check_consumption_coverage({"a": 1.0, "b": 0.6}) == []
    assert check_consumption_coverage({}) == []
