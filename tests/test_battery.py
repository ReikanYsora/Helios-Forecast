"""Tests for the battery state-of-charge projection."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from custom_components.helios_forecast.battery import StepCadenceError, project_battery_soc  # noqa: E402
from custom_components.helios_forecast.config import INF, BatteryConfig  # noqa: E402
from custom_components.helios_forecast.consumption import ConsumptionProfile  # noqa: E402
from custom_components.helios_forecast.forecast import ForecastPoint  # noqa: E402

_UTC = timezone.utc
_NOW = datetime(2026, 1, 5, 12, tzinfo=_UTC)


def _config(**kw) -> BatteryConfig:
    base = dict(
        capacity_kwh=10.0,
        soc_entity="sensor.soc",
        max_charge_w=INF,
        max_discharge_w=INF,
        min_soc_frac=0.0,
        efficiency=0.9,
    )
    base.update(kw)
    return BatteryConfig(**base)


def _flat_load(watts: float) -> ConsumptionProfile:
    return ConsumptionProfile(slot_w={}, hour_w={}, overall_w=watts, samples=1)


def _points(pv_watts: list[float]) -> list[ForecastPoint]:
    return [ForecastPoint(t=_NOW + timedelta(minutes=15 * i), pv_w=w, pv_raw_w=w) for i, w in enumerate(pv_watts)]


def _project(pv_watts, load_w, **cfg):
    return project_battery_soc(_config(**cfg), 0.5, _points(pv_watts), _flat_load(load_w), now=_NOW, tz=_UTC)


def test_surplus_charges_with_efficiency() -> None:
    # 4000 W surplus for 15 min = 1000 Wh at the terminals; sqrt(0.9) is stored.
    soc = _project([4000.0], load_w=0.0)
    stored = 1000.0 * math.sqrt(0.9)
    assert soc[0].soc == round((5000.0 + stored) / 10000.0 * 100.0, 2)


def test_deficit_discharges_with_efficiency() -> None:
    # 4000 W deficit for 15 min = 1000 Wh at the terminals; more is drawn from the cells.
    soc = _project([0.0], load_w=4000.0)
    drawn = 1000.0 / math.sqrt(0.9)
    assert soc[0].soc == round((5000.0 - drawn) / 10000.0 * 100.0, 2)


def test_reserve_floor_holds() -> None:
    soc = _project([0.0], load_w=1_000_000.0, min_soc_frac=0.2)
    assert soc[0].soc == 20.0


def test_full_capacity_caps() -> None:
    soc = _project([1_000_000.0], load_w=0.0)
    assert soc[0].soc == 100.0


def test_charge_power_cap_limits_the_step() -> None:
    # Cap at 500 W: only 125 Wh reaches the terminals in 15 min, whatever the surplus.
    soc = _project([9000.0], load_w=0.0, max_charge_w=500.0)
    stored = 125.0 * math.sqrt(0.9)
    assert soc[0].soc == round((5000.0 + stored) / 10000.0 * 100.0, 2)


def test_unit_efficiency_is_lossless() -> None:
    soc = _project([4000.0], load_w=0.0, efficiency=1.0)
    assert soc[0].soc == round((5000.0 + 1000.0) / 10000.0 * 100.0, 2)


def test_zero_capacity_yields_no_projection() -> None:
    soc = _project([4000.0], load_w=0.0, capacity_kwh=0.0)
    assert soc == []


def test_multistep_accumulation_compounds_across_steps() -> None:
    # Lossless, 1000 W surplus for 15 min = 250 Wh added each step, on top of the previous step's
    # result (not reset every iteration).
    soc = _project([1000.0] * 4, load_w=0.0, efficiency=1.0)
    assert [p.soc for p in soc] == [52.5, 55.0, 57.5, 60.0]


def test_multistep_stays_capped_once_full() -> None:
    # A huge surplus fills a small battery on the first step; later steps must hold at 100%,
    # not overflow the running soc_wh past the capacity clamp.
    soc = _project([50_000.0] * 3, load_w=0.0, efficiency=1.0, capacity_kwh=1.0)
    assert [p.soc for p in soc] == [100.0, 100.0, 100.0]


def test_cadence_mismatch_is_caught_not_silently_mis_integrated() -> None:
    # Points spaced 30 min apart but step_minutes still declares 15: dt_h would silently double
    # the integrated energy for every step. That must raise rather than mis-integrate.
    points = [
        ForecastPoint(t=_NOW, pv_w=4000.0, pv_raw_w=4000.0),
        ForecastPoint(t=_NOW + timedelta(minutes=30), pv_w=4000.0, pv_raw_w=4000.0),
    ]
    with pytest.raises(StepCadenceError):
        project_battery_soc(_config(), 0.5, points, _flat_load(0.0), now=_NOW, tz=_UTC, step_minutes=15)


def test_horizon_window_excludes_out_of_range_points() -> None:
    points = [
        ForecastPoint(t=_NOW - timedelta(minutes=15), pv_w=0.0, pv_raw_w=0.0),  # before now
        ForecastPoint(t=_NOW, pv_w=0.0, pv_raw_w=0.0),
        ForecastPoint(t=_NOW + timedelta(minutes=15), pv_w=0.0, pv_raw_w=0.0),
        ForecastPoint(t=_NOW + timedelta(hours=24), pv_w=0.0, pv_raw_w=0.0),  # at the horizon end (excluded)
    ]
    soc = project_battery_soc(_config(), 0.5, points, _flat_load(0.0), now=_NOW, tz=_UTC)
    assert [p.t for p in soc] == [_NOW, _NOW + timedelta(minutes=15)]
