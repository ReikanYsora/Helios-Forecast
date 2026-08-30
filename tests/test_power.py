"""Tests for the weighted multi-orientation PV layer (compute_pv_power_per_array /
compute_pv_power_weighted), checked against compute_pv_power itself so the two
layers can never silently diverge. Runnable with ``python3 tests/test_power.py``
or under pytest.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.solar.irradiance import (  # noqa: E402
    PanelOrientation,
    PvContext,
    compute_pv_power,
)
from custom_components.helios_forecast.solar.power import (  # noqa: E402
    PvLayout,
    WeatherSample,
    compute_pv_power_per_array,
    compute_pv_power_weighted,
)

_LAT, _LON = 48.8566, 2.3522
_NOON = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)
_NIGHT = datetime(2026, 6, 21, 0, tzinfo=timezone.utc)

_SOUTH = PanelOrientation(tilt_deg=30, azimuth_deg=180)
_EAST = PanelOrientation(tilt_deg=30, azimuth_deg=90)


def _layout(**over) -> PvLayout:
    base = dict(
        orientations=[_SOUTH, _EAST],
        shares=[0.6, 0.4],
        coords=[None, None],
        total_kwp=9.0,
    )
    base.update(over)
    return PvLayout(**base)


def _sample(**over) -> WeatherSample:
    base = dict(cloud=20.0)
    base.update(over)
    return WeatherSample(**base)


# --- compute_pv_power_per_array ---------------------------------------------------


def test_per_array_matches_compute_pv_power_orientation_by_orientation() -> None:
    layout = _layout()
    sample = _sample(temp=18.0, wind=10.0)
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, layout)
    assert len(pcts) == 2
    ctx = PvContext(air_temp_c=18.0, wind_ms=10.0 / 3.6)
    for pct, orientation in zip(pcts, [_SOUTH, _EAST]):
        expected = compute_pv_power(_NOON, _LAT, _LON, sample.cloud, orientation, ctx)
        assert abs(pct - expected) < 1e-12


def test_per_array_uses_home_coords_when_no_override() -> None:
    layout = _layout(coords=[None, None])
    sample = _sample()
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, layout)
    at_home = [compute_pv_power(_NOON, _LAT, _LON, sample.cloud, o, None) for o in [_SOUTH, _EAST]]
    assert pcts == at_home


def test_per_array_applies_per_array_coord_override() -> None:
    # A second array far to the south (Cape Town) must diverge from the home-latitude figure
    # at the same instant, and must equal compute_pv_power evaluated at ITS OWN coordinates.
    far_lat, far_lon = -33.9, 18.4
    layout = _layout(coords=[None, (far_lat, far_lon)])
    sample = _sample()
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, layout)
    expected_far = compute_pv_power(_NOON, far_lat, far_lon, sample.cloud, _EAST, None)
    assert abs(pcts[1] - expected_far) < 1e-12
    assert pcts[1] != pcts[0]


def test_per_array_builds_no_ctx_when_weather_is_bare() -> None:
    # cloud-only sample: no temp/wind/ghi/direct/diffuse means base_ctx must stay None,
    # so the per-array result must equal calling compute_pv_power with ctx=None directly
    # (not, say, a PvContext with a phantom air_temp_c of 0).
    layout = _layout()
    sample = _sample()
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, layout)
    bare = [compute_pv_power(_NOON, _LAT, _LON, sample.cloud, o, None) for o in [_SOUTH, _EAST]]
    assert pcts == bare


def test_per_array_converts_wind_kmh_to_ms() -> None:
    # WeatherSample.wind is km/h (matches the Open-Meteo field the caller passes through);
    # compute_pv_power_per_array must divide by 3.6 before handing it to PvContext.
    layout = _layout()
    sample = _sample(temp=30.0, wind=36.0)  # 36 km/h == 10 m/s
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, layout)
    ctx = PvContext(air_temp_c=30.0, wind_ms=10.0)
    expected = [compute_pv_power(_NOON, _LAT, _LON, sample.cloud, o, ctx) for o in [_SOUTH, _EAST]]
    assert pcts == expected


def test_per_array_night_is_all_zero() -> None:
    layout = _layout()
    pcts = compute_pv_power_per_array(_NIGHT, _LAT, _LON, _sample(), layout)
    assert pcts == [0.0, 0.0]


def test_per_array_fallback_on_empty_layout() -> None:
    empty = PvLayout(orientations=[], shares=[], coords=[], total_kwp=0.0)
    sample = _sample()
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, empty)
    assert pcts == [compute_pv_power(_NOON, _LAT, _LON, sample.cloud, None, None)]


def test_per_array_fallback_on_out_of_lockstep_shares() -> None:
    # Defensive branch: shares shorter than orientations must not raise, and must fall
    # back to the single horizontal-path element rather than indexing off the end.
    bad = _layout(shares=[1.0])
    sample = _sample()
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, bad)
    assert pcts == [compute_pv_power(_NOON, _LAT, _LON, sample.cloud, None, None)]


def test_per_array_fallback_on_out_of_lockstep_coords() -> None:
    bad = _layout(coords=[None])
    sample = _sample()
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, bad)
    assert pcts == [compute_pv_power(_NOON, _LAT, _LON, sample.cloud, None, None)]


# --- compute_pv_power_weighted ----------------------------------------------------


def test_weighted_matches_manual_share_sum() -> None:
    layout = _layout(shares=[0.6, 0.4])
    sample = _sample(temp=22.0)
    got = compute_pv_power_weighted(_NOON, _LAT, _LON, sample, layout)
    pcts = compute_pv_power_per_array(_NOON, _LAT, _LON, sample, layout)
    expected = pcts[0] * 0.6 + pcts[1] * 0.4
    assert abs(got - expected) < 1e-12


def test_weighted_equal_orientations_equal_shares_matches_single() -> None:
    # Two identical orientations at equal 0.5 shares must reproduce the single-orientation figure.
    layout = _layout(orientations=[_SOUTH, _SOUTH], shares=[0.5, 0.5])
    sample = _sample()
    got = compute_pv_power_weighted(_NOON, _LAT, _LON, sample, layout)
    solo = compute_pv_power(_NOON, _LAT, _LON, sample.cloud, _SOUTH, None)
    assert abs(got - solo) < 1e-12


def test_weighted_fallback_on_empty_layout() -> None:
    empty = PvLayout(orientations=[], shares=[], coords=[], total_kwp=0.0)
    sample = _sample()
    got = compute_pv_power_weighted(_NOON, _LAT, _LON, sample, empty)
    assert got == compute_pv_power(_NOON, _LAT, _LON, sample.cloud, None, None)


def test_weighted_night_is_zero() -> None:
    layout = _layout()
    assert compute_pv_power_weighted(_NIGHT, _LAT, _LON, _sample(), layout) == 0.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all power tests passed")
