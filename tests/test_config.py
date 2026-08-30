"""Tests for the config-entry to model-inputs mapping."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.config import (  # noqa: E402
    CONF_ARRAYS,
    INF,
    battery_from_config,
    inverter_max_w_from_config,
    layout_from_config,
    learning_from_config,
    lines_from_config,
    location_from_config,
    merge_entry_data,
    split_line,
    split_settings,
    trend_anchor_hour_from_config,
)


def test_layout_shares_normalised_by_kwp() -> None:
    layout = layout_from_config(
        {"arrays": [{"tilt": 30, "azimuth": 90, "kwp": 4}, {"tilt": 30, "azimuth": 270, "kwp": 2}]}
    )
    assert layout.total_kwp == 6.0
    assert abs(layout.shares[0] - 4 / 6) < 1e-12
    assert abs(layout.shares[1] - 2 / 6) < 1e-12
    assert layout.orientations[0].azimuth_deg == 90
    assert layout.orientations[1].tracker is None
    assert layout.coords == [None, None]


def test_layout_per_array_coords_and_tracker() -> None:
    layout = layout_from_config(
        {"arrays": [{"tilt": 0, "azimuth": 180, "kwp": 5, "tracker": "dual-axis", "latitude": 1.5, "longitude": 2.5}]}
    )
    assert layout.coords == [(1.5, 2.5)]
    assert layout.orientations[0].tracker == "dual-axis"


def test_layout_empty() -> None:
    layout = layout_from_config({})
    assert layout.orientations == []
    assert layout.total_kwp == 0.0


def test_inverter_cap() -> None:
    assert inverter_max_w_from_config({"inverter_max_kw": 5}) == 5000.0
    assert inverter_max_w_from_config({"inverter_max_kw": 0}) == INF
    assert inverter_max_w_from_config({}) == INF


def test_location_override_else_home() -> None:
    assert location_from_config({"latitude": 10.0, "longitude": 20.0}, 48.0, 2.0) == (10.0, 20.0)
    assert location_from_config({}, 48.0, 2.0) == (48.0, 2.0)
    assert location_from_config({"latitude": 10.0}, 48.0, 2.0) == (48.0, 2.0)  # incomplete -> home


def test_split_line_keeps_only_geometry_and_drops_empty() -> None:
    form = {
        "tilt": 30,
        "azimuth": 180,
        "kwp": 2.61,
        "tracker": "none",
        "latitude": 1.0,  # entry-level, not a line field
        "add_another": True,  # control key, never stored
    }
    assert split_line(form) == {"tilt": 30, "azimuth": 180, "kwp": 2.61, "tracker": "none"}
    # Missing / None geometry values are dropped rather than stored as null.
    assert split_line({"tilt": 30, "azimuth": None}) == {"tilt": 30}


def test_split_settings_keeps_only_settings() -> None:
    form = {"tilt": 30, "kwp": 2.61, "latitude": 1.0, "longitude": 2.0, "inverter_max_kw": 5.5}
    assert split_settings(form) == {"latitude": 1.0, "longitude": 2.0, "inverter_max_kw": 5.5}


def test_merge_and_read_back_lines_roundtrip() -> None:
    settings = {"latitude": 1.0, "inverter_max_kw": 8.0}
    lines = [{"tilt": 30, "azimuth": 90, "kwp": 6.0}, {"tilt": 30, "azimuth": 270, "kwp": 3.0}]
    data = merge_entry_data(settings, lines)
    assert data[CONF_ARRAYS] == lines
    assert data["inverter_max_kw"] == 8.0
    assert lines_from_config(data) == lines
    assert lines_from_config({}) == []


def test_multi_line_entry_shares_one_inverter_cap() -> None:
    # Two strings on one 8 kW inverter (issue #18): the entry-level cap applies to the
    # combined output, and the layout sums them by kWp share.
    data = merge_entry_data(
        {"inverter_max_kw": 8.0},
        [{"tilt": 30, "azimuth": 90, "kwp": 6.0}, {"tilt": 30, "azimuth": 270, "kwp": 3.0}],
    )
    layout = layout_from_config(data)
    assert layout.total_kwp == 9.0
    assert abs(layout.shares[0] - 6 / 9) < 1e-12
    assert inverter_max_w_from_config(data) == 8000.0


def test_decimal_peak_power_preserved() -> None:
    # Regression for issue #13: a decimal kWp must survive into the layout at full precision.
    layout = layout_from_config(merge_entry_data({}, [{"tilt": 30, "azimuth": 180, "kwp": 2.61}]))
    assert layout.total_kwp == 2.61


def test_layout_no_line_caps_when_none_set() -> None:
    layout = layout_from_config(
        {"arrays": [{"tilt": 30, "azimuth": 90, "kwp": 4}, {"tilt": 30, "azimuth": 270, "kwp": 2}]}
    )
    assert layout.caps == []


def test_layout_line_caps_mixed_capped_and_uncapped() -> None:
    layout = layout_from_config(
        {
            "arrays": [
                {"tilt": 30, "azimuth": 90, "kwp": 4, "line_inverter_max_kw": 3.0},
                {"tilt": 30, "azimuth": 270, "kwp": 2},
            ]
        }
    )
    assert layout.caps == [3000.0, INF]


def test_layout_line_cap_zero_or_negative_is_uncapped() -> None:
    layout = layout_from_config({"arrays": [{"tilt": 30, "azimuth": 90, "kwp": 4, "line_inverter_max_kw": 0}]})
    assert layout.caps == []


def test_learning_entity_or_none() -> None:
    assert learning_from_config({"production_entity": "sensor.pv"}) == "sensor.pv"
    assert learning_from_config({}) is None
    assert learning_from_config({"production_entity": ""}) is None


def test_trend_anchor_hour_default_and_clamped() -> None:
    assert trend_anchor_hour_from_config({}) == 6
    assert trend_anchor_hour_from_config({"trend_anchor_hour": 14}) == 14
    # Out-of-range inputs are clamped rather than rejected.
    assert trend_anchor_hour_from_config({"trend_anchor_hour": -5}) == 0
    assert trend_anchor_hour_from_config({"trend_anchor_hour": 99}) == 23


def test_battery_off_without_capacity_or_soc_entity() -> None:
    assert battery_from_config({}) is None
    assert battery_from_config({"battery_capacity_kwh": 10.0}) is None  # no SoC entity
    assert battery_from_config({"battery_soc_entity": "sensor.soc"}) is None  # no capacity
    assert battery_from_config({"battery_capacity_kwh": 0, "battery_soc_entity": "sensor.soc"}) is None


def test_battery_on_with_defaults() -> None:
    config = battery_from_config({"battery_capacity_kwh": 10.0, "battery_soc_entity": "sensor.soc"})
    assert config is not None
    assert config.capacity_kwh == 10.0
    assert config.soc_entity == "sensor.soc"
    assert config.max_charge_w == INF
    assert config.max_discharge_w == INF
    assert config.min_soc_frac == 0.1  # default 10%
    assert config.efficiency == 0.9  # default 90%


def test_battery_on_with_explicit_fields() -> None:
    config = battery_from_config(
        {
            "battery_capacity_kwh": 5.0,
            "battery_soc_entity": "sensor.soc",
            "battery_max_charge_kw": 2.0,
            "battery_max_discharge_kw": 3.0,
            "battery_min_soc": 20,
            "battery_efficiency": 95,
        }
    )
    assert config is not None
    assert config.max_charge_w == 2000.0
    assert config.max_discharge_w == 3000.0
    assert config.min_soc_frac == 0.2
    assert config.efficiency == 0.95


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all config tests passed")
