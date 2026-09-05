"""Tests for the benchmark payload: what leaves an installation that opted in, and what never does."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.benchmark import (  # noqa: E402
    COORD_DECIMALS,
    OBSERVED_HOURS,
    SCHEMA_VERSION,
    build_payload,
    site_id,
)
from custom_components.helios_forecast.forecast import ForecastPoint  # noqa: E402
from custom_components.helios_forecast.reliability import Reliability  # noqa: E402
from custom_components.helios_forecast.solar.residual import ProductionBucket  # noqa: E402

_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
_H = 3_600_000.0


def _bucket(hours_ago: float, kwh: float, curtailed: bool = False) -> ProductionBucket:
    start = (_NOW - timedelta(hours=hours_ago)).timestamp() * 1000.0
    return ProductionBucket(start_ms=start, end_ms=start + _H, kwh=kwh, curtailed=curtailed)


def _payload(**overrides):
    args = dict(
        entry_id="01KZ0S5BNAQR1H29CYZXVDC9EE",
        version="2026.10.0",
        emitted_at=_NOW,
        latitude=44.109959823,
        longitude=1.404356360,
        lines=[{"azimuth": 188.0, "tilt": 13.0, "kwp": 3.0, "tracker": None}],
        inverter_max_kw=3.0,
        points=[ForecastPoint(t=_NOW, pv_w=1781.8247, pv_raw_w=1900.5, pv_p10=1548.7, pv_p90=2143.27)],
        reliability=Reliability(
            overall=87.4, data_maturity=0.5, recent_skill=0.9, today_predictability=0.6, days_learned=30, per_day=[]
        ),
        production=[_bucket(1, 1.9), _bucket(2, 2.4, curtailed=True)],
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
    assert set(p) == {"schema", "site_id", "emitted_at", "model_version", "site", "reliability", "forecast", "observed"}
    assert p["schema"] == SCHEMA_VERSION
    assert p["model_version"] == "2026.10.0"
    assert set(p["site"]) == {
        "latitude",
        "longitude",
        "inverter_max_kw",
        "has_battery",
        "has_curtailment_signal",
        "lines",
    }
    assert set(p["site"]["lines"][0]) == {"azimuth", "tilt", "kwp", "tracker"}
    # The blended value, the pure physical model beside it, and the band: enough to score an ablation
    # without asking the installation to run one.
    assert p["forecast"][0] == {"t": _NOW.isoformat(), "w": 1781.82, "raw_w": 1900.5, "p10": 1548.7, "p90": 2143.27}
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
