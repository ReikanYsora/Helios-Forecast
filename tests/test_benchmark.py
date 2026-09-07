"""Tests for the benchmark payload: what leaves an installation that opted in, and what never does."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.benchmark import (  # noqa: E402
    COORD_DECIMALS,
    DENSE_HOURS,
    OBSERVED_HOURS,
    SCHEMA_VERSION,
    build_payload,
    site_id,
)
from custom_components.helios_forecast.forecast import ForecastPoint  # noqa: E402
from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402
from custom_components.helios_forecast.reliability import Reliability  # noqa: E402
from custom_components.helios_forecast.solar.residual import ProductionBucket  # noqa: E402

_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
_H = 3_600_000.0


def _bucket(hours_ago: float, kwh: float, curtailed: bool = False) -> ProductionBucket:
    start = (_NOW - timedelta(hours=hours_ago)).timestamp() * 1000.0
    return ProductionBucket(start_ms=start, end_ms=start + _H, kwh=kwh, curtailed=curtailed)


def _weather(hours: int = 4, first: datetime = _NOW - timedelta(hours=1)) -> WeatherSeries:
    """A short hourly window shaped like the real one, one value per field per hour."""
    times = [first + timedelta(hours=i) for i in range(hours)]
    return WeatherSeries(
        times=times,
        cloud=[10.0 * i for i in range(hours)],
        shortwave=[100.0 * i for i in range(hours)],
        direct=[80.0 * i for i in range(hours)],
        diffuse=[20.0 * i for i in range(hours)],
        temp=[15.0 + i for i in range(hours)],
        wind=[5.0] * hours,
        snow=[0.0] * hours,
        cloud_spread=[1.5] * hours,
        elevation_m=137.0,
    )


def _payload(**overrides):
    args = dict(
        entry_id="01KZ0S5BNAQR1H29CYZXVDC9EE",
        version="2026.9.2",
        emitted_at=_NOW,
        latitude=44.109959823,
        longitude=1.404356360,
        lines=[{"azimuth": 188.0, "tilt": 13.0, "kwp": 3.0, "tracker": None}],
        country="FR",
        time_zone="Europe/Paris",
        inverter_max_kw=3.0,
        points=[ForecastPoint(t=_NOW, pv_w=1781.8247, pv_raw_w=1900.5, pv_p10=1548.7, pv_p90=2143.27, cloud=42.0)],
        reliability=Reliability(
            overall=87.4, data_maturity=0.5, recent_skill=0.9, today_predictability=0.6, days_learned=30, per_day=[]
        ),
        production=[_bucket(1, 1.9), _bucket(2, 2.4, curtailed=True)],
        weather=_weather(),
        learning={
            "production_hours": 2,
            "curtailed_hours": 1,
            "analog_samples": 40,
            "sky_cells": 91,
            "sky_cells_total": 648,
            "residual_global": 0.97,
            "learn_days": 60,
        },
        has_battery=False,
        has_curtailment_signal=True,
    )
    args.update(overrides)
    return build_payload(**args)


def test_site_id_is_stable_and_says_nothing_about_the_installation() -> None:
    first = site_id("entry-a")
    assert first == site_id("entry-a")
    assert first != site_id("entry-b")
    assert "entry-a" not in first
    assert len(first) == 16


def test_the_payload_carries_the_prediction_the_geometry_and_nothing_else() -> None:
    p = _payload()
    assert set(p) == {
        "schema",
        "site_id",
        "emitted_at",
        "model_version",
        "site",
        "reliability",
        "forecast",
        "weather",
        "learning",
        "observed",
    }
    assert p["schema"] == SCHEMA_VERSION
    assert p["model_version"] == "2026.9.2"
    assert set(p["site"]) == {
        "latitude",
        "longitude",
        "country",
        "time_zone",
        "elevation_m",
        "inverter_max_kw",
        "has_battery",
        "has_curtailment_signal",
        "lines",
    }
    assert p["site"]["country"] == "FR"
    assert set(p["site"]["lines"][0]) == {"azimuth", "tilt", "kwp", "tracker"}
    # The blended value, the pure physical model beside it, and the band: enough to score an ablation
    # without asking the installation to run one.
    assert p["forecast"][0] == {
        "t": _NOW.isoformat(),
        "w": 1781.82,
        "raw_w": 1900.5,
        "p10": 1548.7,
        "p90": 2143.27,
        "cloud": 42.0,
        "conf": None,
    }
    assert p["observed"][0]["kwh"] == 1.9
    assert p["observed"][1]["curtailed"] is True


def test_coordinates_leave_rounded_to_about_a_kilometre() -> None:
    site = _payload()["site"]
    assert site["latitude"] == round(44.109959823, COORD_DECIMALS)
    assert site["longitude"] == round(1.404356360, COORD_DECIMALS)
    # Rounded, not truncated to the home: the address is gone, the weather grid cell is not.
    assert abs(site["latitude"] - 44.109959823) < 0.01
    assert abs(site["longitude"] - 1.404356360) < 0.01


def test_a_thin_band_travels_as_nothing_rather_than_as_a_number() -> None:
    point = ForecastPoint(t=_NOW, pv_w=10.0, pv_raw_w=12.0)
    p = _payload(points=[point])
    assert p["forecast"][0]["p10"] is None
    assert p["forecast"][0]["p90"] is None


def test_only_the_recent_past_is_repeated_on_every_emission() -> None:
    production = [_bucket(1, 1.0), _bucket(OBSERVED_HOURS - 1, 2.0), _bucket(OBSERVED_HOURS + 5, 3.0)]
    observed = _payload(production=production)["observed"]
    assert [o["kwh"] for o in observed] == [1.0, 2.0]


def test_an_installation_with_no_history_still_emits() -> None:
    p = _payload(production=[], points=[])
    assert p["observed"] == []
    assert p["forecast"] == []
    assert p["site_id"]


# --- what schema 2 added, so an error can be attributed and not only measured -------------------


def test_the_weather_the_model_read_travels_with_the_prediction() -> None:
    rows = _payload()["weather"]
    # The hour before the emission is inside the observed window and is kept; every field the
    # physics consumes is there, plus the ensemble's own disagreement.
    assert [r["t"] for r in rows] == [(_NOW + timedelta(hours=i)).isoformat() for i in (-1, 0, 1, 2)]
    assert set(rows[0]) == {"t", "ghi", "direct", "diffuse", "temp", "wind", "snow", "cloud", "cloud_spread"}
    assert rows[1]["ghi"] == 100.0 and rows[1]["cloud"] == 10.0 and rows[1]["temp"] == 16.0


def test_weather_older_than_the_observed_window_is_not_repeated() -> None:
    far = _weather(hours=3, first=_NOW - timedelta(hours=OBSERVED_HOURS + 5))
    assert _payload(weather=far)["weather"] == []


def test_an_installation_whose_weather_fetch_failed_still_emits() -> None:
    p = _payload(weather=None)
    assert p["weather"] == []
    assert p["site"]["elevation_m"] is None
    assert p["forecast"]


def test_a_short_weather_array_leaves_a_null_rather_than_shifting_the_others() -> None:
    weather = _weather()
    weather.snow.clear()
    rows = _payload(weather=weather)["weather"]
    assert all(r["snow"] is None for r in rows)
    assert rows[1]["ghi"] == 100.0


def test_the_ground_elevation_and_the_local_zone_travel() -> None:
    site = _payload()["site"]
    assert site["elevation_m"] == 137.0
    assert site["time_zone"] == "Europe/Paris"


def test_the_analog_confidence_of_each_point_travels() -> None:
    point = ForecastPoint(t=_NOW, pv_w=10.0, pv_raw_w=12.0, analog_confidence=0.6421)
    assert _payload(points=[point])["forecast"][0]["conf"] == 0.642


def test_what_the_learning_stood_on_travels_unchanged() -> None:
    learning = {
        "production_hours": 1440,
        "curtailed_hours": 12,
        "analog_samples": 700,
        "sky_cells": 91,
        "sky_cells_total": 648,
        "residual_global": 0.97,
        "learn_days": 60,
    }
    assert _payload(learning=learning)["learning"] == learning


def test_the_near_curve_travels_at_its_native_step_and_the_far_one_hourly() -> None:
    # An hour is scored against the meter's reading for the hour that contains it, so four points
    # inside one hour are four comparisons with the same truth: past the near term they are weight
    # without information. Near term keeps them, where a battery decision turns on them.
    points = [ForecastPoint(t=_NOW + timedelta(minutes=15 * i), pv_w=1.0, pv_raw_w=1.0) for i in range(7 * 96)]
    curve = _payload(points=points)["forecast"]
    stamps = [datetime.fromisoformat(row["t"]) for row in curve]
    edge = _NOW + timedelta(hours=DENSE_HOURS)
    near = [t for t in stamps if t < edge]
    far = [t for t in stamps if t >= edge]
    assert len(near) == DENSE_HOURS * 4
    assert all(t.minute == 0 for t in far)
    assert far == [edge + timedelta(hours=i) for i in range(len(far))]
    # Nothing is dropped from the near term, and the far term runs to the last whole hour the curve
    # reaches: the quarter-hours after it are exactly what the thinning is for.
    assert stamps[0] == _NOW
    assert stamps[-1] == max(p.t for p in points if p.t.minute == 0)


def test_a_curve_already_hourly_travels_whole() -> None:
    points = [ForecastPoint(t=_NOW + timedelta(hours=i), pv_w=1.0, pv_raw_w=1.0) for i in range(48)]
    assert len(_payload(points=points)["forecast"]) == 48


def test_a_point_with_no_readable_time_is_dropped_rather_than_guessed_at() -> None:
    class _Broken:
        t = None
        pv_w = pv_raw_w = 1.0
        pv_p10 = pv_p90 = cloud = None

    good = ForecastPoint(t=_NOW, pv_w=1.0, pv_raw_w=1.0)
    curve = _payload(points=[good, _Broken()])["forecast"]
    assert [row["t"] for row in curve] == [_NOW.isoformat()]
