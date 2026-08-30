"""Tests for deriving the home consumption profile from the Energy dashboard."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.consumption import (  # noqa: E402
    ConsumptionSources,
    build_consumption_profile,
    consumption_sources,
)
from custom_components.helios_forecast.solar.residual import ProductionBucket  # noqa: E402

_UTC = timezone.utc


def _bucket(dt: datetime, kwh: float) -> ProductionBucket:
    ms = dt.timestamp() * 1000.0
    return ProductionBucket(start_ms=ms, end_ms=ms + 3600_000.0, kwh=kwh)


def test_sources_signs_solar_grid_battery() -> None:
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.pv"},
            {"type": "battery", "stat_energy_from": "sensor.bat_out", "stat_energy_to": "sensor.bat_in"},
            {"type": "grid", "stat_energy_from": "sensor.import", "stat_energy_to": "sensor.export"},
        ]
    }
    signed = consumption_sources(prefs).signed
    assert signed == {
        "sensor.pv": 1,
        "sensor.bat_out": 1,  # discharge adds to consumption
        "sensor.bat_in": -1,  # charge subtracts
        "sensor.import": 1,
        "sensor.export": -1,
    }


def test_sources_legacy_grid_flow_lists() -> None:
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.import"}],
                "flow_to": [{"stat_energy_to": "sensor.export"}],
            }
        ]
    }
    assert consumption_sources(prefs).signed == {"sensor.import": 1, "sensor.export": -1}


def test_sources_missing_pieces_drop_out() -> None:
    assert consumption_sources(None).signed == {}
    assert consumption_sources({}).signed == {}
    # A grid source with only import configured yields just that id.
    prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.import", "stat_energy_to": None}]}
    assert consumption_sources(prefs).signed == {"sensor.import": 1}


def test_profile_signed_sum_per_hour_to_watts() -> None:
    # Monday 10:00, two ids: +1 (production 3 kWh) and -1 (export 1 kWh) -> net 2 kWh -> 2000 W.
    monday_10 = datetime(2026, 1, 5, 10, tzinfo=_UTC)
    sources = ConsumptionSources(signed={"a": 1, "b": -1})
    buckets = {"a": [_bucket(monday_10, 3.0)], "b": [_bucket(monday_10, 1.0)]}
    profile = build_consumption_profile(sources, buckets, _UTC)
    assert profile is not None
    assert abs(profile.at(monday_10) - 2000.0) < 1e-9


def test_profile_floors_negative_net_at_zero() -> None:
    monday_10 = datetime(2026, 1, 5, 10, tzinfo=_UTC)
    sources = ConsumptionSources(signed={"a": 1, "b": -1})
    buckets = {"a": [_bucket(monday_10, 1.0)], "b": [_bucket(monday_10, 3.0)]}  # net -2 kWh
    profile = build_consumption_profile(sources, buckets, _UTC)
    assert profile is not None
    assert profile.at(monday_10) == 0.0


def test_profile_averages_same_slot_and_falls_back() -> None:
    m1 = datetime(2026, 1, 5, 10, tzinfo=_UTC)  # Monday 10:00
    m2 = datetime(2026, 1, 12, 10, tzinfo=_UTC)  # next Monday 10:00, same slot
    sources = ConsumptionSources(signed={"load": 1})
    buckets = {"load": [_bucket(m1, 1.0), _bucket(m2, 2.0)]}
    profile = build_consumption_profile(sources, buckets, _UTC)
    assert profile is not None
    # Slot (Mon 10) is the average of 1000 and 2000 W.
    assert abs(profile.at(m1) - 1500.0) < 1e-9
    # Same hour, different weekday: no slot, falls back to the hour-10 average.
    tuesday_10 = datetime(2026, 1, 6, 10, tzinfo=_UTC)
    assert abs(profile.at(tuesday_10) - 1500.0) < 1e-9
    # Unknown hour: falls back to the overall average.
    tuesday_3 = datetime(2026, 1, 6, 3, tzinfo=_UTC)
    assert abs(profile.at(tuesday_3) - 1500.0) < 1e-9


def test_profile_partial_source_missing_from_buckets() -> None:
    # "b" is signed but never fetched (e.g. a failed history call): it must drop out silently
    # rather than crash, leaving the profile built from whatever sources did come back.
    monday_10 = datetime(2026, 1, 5, 10, tzinfo=_UTC)
    sources = ConsumptionSources(signed={"a": 1, "b": -1})
    buckets = {"a": [_bucket(monday_10, 3.0)]}
    profile = build_consumption_profile(sources, buckets, _UTC)
    assert profile is not None
    assert abs(profile.at(monday_10) - 3000.0) < 1e-9


def test_profile_none_without_history() -> None:
    sources = ConsumptionSources(signed={"load": 1})
    assert build_consumption_profile(sources, {}, _UTC) is None
