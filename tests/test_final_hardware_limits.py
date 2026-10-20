"""Final analog output honors hardware rather than today's physical-model watts."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.helios_forecast.analog import AnalogSample, enrich_archive_points, enrich_points  # noqa: E402
from custom_components.helios_forecast.forecast import (  # noqa: E402
    ForecastPoint,
    build_forecast_series,
    hardware_power_limit_w,
    limit_forecast_power,
)
from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402
from custom_components.helios_forecast.solar.geometry import sun_position  # noqa: E402
from custom_components.helios_forecast.solar.irradiance import PanelOrientation  # noqa: E402
from custom_components.helios_forecast.solar.power import PvLayout  # noqa: E402

NOON = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)
LAT, LON = 45.0, 0.0


def _layout(caps: list[float], shares: list[float] | None = None) -> PvLayout:
    shares = shares if shares is not None else [1.0 / len(caps)] * len(caps)
    return PvLayout(
        orientations=[PanelOrientation(30.0, 180.0) for _ in shares],
        shares=shares,
        coords=[None] * len(shares),
        total_kwp=10.0,
        caps=caps,
    )


def _weather() -> WeatherSeries:
    return WeatherSeries(
        times=[NOON, NOON + timedelta(hours=1)],
        cloud=[30.0, 30.0],
        shortwave=[1000.0, 1000.0],
        direct=[700.0, 700.0],
        diffuse=[300.0, 300.0],
        temp=[20.0, 20.0],
        wind=[5.0, 5.0],
        snow=[0.0, 0.0],
    )


def _library(ratio: float | None = 1.4) -> list[AnalogSample]:
    sun = sun_position(NOON, LAT, LON)
    return [AnalogSample(sun.altitude, sun.azimuth, 30.0, 6300.0, 20.0, ratio) for _ in range(40)]


@pytest.mark.parametrize("archive", [False, True])
def test_analog_cannot_undo_the_physical_inverter_clip(monkeypatch, archive: bool) -> None:
    monkeypatch.setattr("custom_components.helios_forecast.forecast.compute_pv_power_per_array", lambda *args: [100.0])
    layout = _layout([math.inf])
    points = build_forecast_series(
        _weather(), layout, LAT, LON, inverter_max_w=4500.0, start=NOON, end=NOON + timedelta(minutes=15)
    )
    assert points[0].pv_raw_w == points[0].pv_w == 4500.0
    cap = hardware_power_limit_w(layout, 4500.0)
    if archive:
        result = enrich_archive_points(points, _library(), _weather(), LAT, LON, hardware_max_w=cap)
    else:
        result = enrich_points(points, _library(), _weather(), LAT, LON, NOON, hardware_max_w=cap)
    point = result[0]
    assert point.pv_w == point.pv_p10 == point.pv_p90 == 4500.0
    # Without a hardware bound the historical API/formula remains unchanged.
    assert enrich_points(points, _library(), _weather(), LAT, LON, NOON)[0].pv_w == 6300.0


@pytest.mark.parametrize(
    ("caps", "shares", "entry", "expected"),
    [
        ([2000.0, 3000.0], None, math.inf, 5000.0),
        ([2000.0, 3000.0], None, 4500.0, 4500.0),
        ([2000.0, 3000.0], None, 8000.0, 5000.0),
        ([2000.0, math.inf], None, 8000.0, 8000.0),
        ([2000.0, math.inf], None, math.inf, math.inf),
        ([2000.0], [0.5, 0.5], math.inf, math.inf),
        ([2000.0, math.inf], [1.0, 0.0], math.inf, 2000.0),
        ([2000.0, math.inf], [0.0, 0.0], math.inf, 0.0),
        ([], [1.0], math.inf, math.inf),
    ],
)
def test_combined_hardware_ceiling(caps, shares, entry, expected) -> None:
    assert hardware_power_limit_w(_layout(caps, shares), entry) == expected


@pytest.mark.parametrize("ratio", [None, 1.4])
def test_per_line_total_bounds_both_analog_paths(ratio: float | None) -> None:
    point = ForecastPoint(NOON, 4500.0, 4500.0)
    cap = hardware_power_limit_w(_layout([2000.0, 3000.0]))
    result = enrich_points([point], _library(ratio), _weather(), LAT, LON, NOON, cap)[0]
    assert result.pv_w == result.pv_p10 == result.pv_p90 == 5000.0


@pytest.mark.parametrize(("entry", "expected"), [(math.inf, 5000.0), (4500.0, 4500.0)])
def test_final_bound_composes_the_same_line_and_entry_caps_as_physics(monkeypatch, entry, expected) -> None:
    monkeypatch.setattr(
        "custom_components.helios_forecast.forecast.compute_pv_power_per_array", lambda *args: [100.0, 100.0]
    )
    layout = _layout([2000.0, 3000.0])
    points = build_forecast_series(
        _weather(), layout, LAT, LON, inverter_max_w=entry, start=NOON, end=NOON + timedelta(minutes=15)
    )
    assert points[0].pv_w == points[0].pv_raw_w == expected
    result = enrich_points(
        points, _library(), _weather(), LAT, LON, NOON, hardware_max_w=hardware_power_limit_w(layout, entry)
    )[0]
    assert result.pv_w == result.pv_p10 == result.pv_p90 == expected


def test_raw_output_below_cap_is_not_an_artificial_ceiling_or_double_correction() -> None:
    point = ForecastPoint(NOON, 800.0, 1000.0)
    # The measured/model ratio is already the site correction. It multiplies raw,
    # not the residual-corrected 800 W, even when the final cap is present.
    result = enrich_points([point], _library(0.8), _weather(), LAT, LON, NOON, 4500.0)[0]
    assert result.pv_w == result.pv_p10 == result.pv_p90 == 800.0
    raised = enrich_points([point], _library(1.4), _weather(), LAT, LON, NOON, 4500.0)[0]
    assert raised.pv_w == 1400.0 > point.pv_raw_w


@pytest.mark.parametrize("archive", [False, True])
def test_cold_start_clips_watts_and_preserves_null_bands(archive: bool) -> None:
    point = ForecastPoint(NOON, 6300.0, 4500.0)
    if archive:
        result = enrich_archive_points([point], [], _weather(), LAT, LON, 4500.0)[0]
    else:
        result = enrich_points([point], [], _weather(), LAT, LON, NOON, 4500.0)[0]
    assert result.pv_w == 4500.0
    assert result.pv_p10 is None and result.pv_p90 is None


def test_live_past_points_keep_existing_bands_but_obey_hardware() -> None:
    point = ForecastPoint(NOON, 6300.0, 4500.0, 5000.0, 7000.0)
    result = enrich_points([point], _library(), _weather(), LAT, LON, NOON + timedelta(hours=1), 4500.0)[0]
    assert result.pv_w == result.pv_p10 == result.pv_p90 == 4500.0


def test_thin_analog_support_does_not_invent_bands_when_capped() -> None:
    result = enrich_points([ForecastPoint(NOON, 6300.0, 4500.0)], _library()[:1], _weather(), LAT, LON, NOON, 4500.0)[0]
    assert result.pv_w == 4500.0
    assert result.pv_p10 is None and result.pv_p90 is None


@pytest.mark.parametrize("invalid", [math.nan, -1.0, -math.inf])
def test_invalid_hardware_bound_fails_closed(invalid: float) -> None:
    with pytest.raises(ValueError, match="Hardware power limit"):
        limit_forecast_power(ForecastPoint(NOON, 100.0, 100.0), invalid)
    with pytest.raises(ValueError, match="Hardware power limit"):
        hardware_power_limit_w(_layout([2000.0]), invalid)
    with pytest.raises(ValueError, match="Hardware power limit"):
        hardware_power_limit_w(_layout([invalid]))


def test_zero_hardware_bound_keeps_null_band_null() -> None:
    point = limit_forecast_power(ForecastPoint(NOON, 100.0, 100.0, None, 150.0), 0.0)
    assert point.pv_w == point.pv_p90 == 0.0
    assert point.pv_p10 is None


@pytest.mark.parametrize("invalid", [math.nan, -0.1, math.inf])
def test_invalid_line_share_cannot_imply_a_hardware_ceiling(invalid: float) -> None:
    with pytest.raises(ValueError, match="PV layout shares"):
        hardware_power_limit_w(_layout([2000.0], [invalid]))
