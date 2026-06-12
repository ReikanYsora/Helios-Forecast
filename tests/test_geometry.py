"""Parity test for the solar position port.

Checks custom_components/helios_forecast/solar/geometry.sun_position against
golden values emitted by the card's own getSunPosition (see
tests/fixtures/_gen_sun_position.mjs). Same formula on both sides, so the match
must be to floating-point precision, not just "close". Runnable directly with
``python3 tests/test_geometry.py`` or under pytest.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.solar.geometry import sun_position  # noqa: E402

_FIXTURES = json.loads((_REPO_ROOT / "tests" / "fixtures" / "sun_position.json").read_text())

# Same IEEE-754 doubles and libm on both sides, so the only slack is last-bit
# rounding in sin/cos/asin/acos. 1e-6 deg is ~0.0036 arc-seconds, four orders
# below the model's own 0.3 deg accuracy: anything looser would hide a real port
# bug, anything tighter would chase libm noise.
_TOL_DEG = 1e-6


def _max_errors() -> tuple[float, float, dict]:
    max_alt = 0.0
    max_az = 0.0
    worst: dict = {}
    for fx in _FIXTURES:
        moment = datetime(fx["year"], fx["month"], fx["day"], fx["hour"], tzinfo=timezone.utc)
        got = sun_position(moment, fx["lat"], fx["lon"])
        d_alt = abs(got.altitude - fx["altitude"])
        d_az = abs(got.azimuth - fx["azimuth"])
        if d_alt > max_alt or d_az > max_az:
            worst = {"fixture": fx, "got": got, "d_alt": d_alt, "d_az": d_az}
        max_alt = max(max_alt, d_alt)
        max_az = max(max_az, d_az)
    return max_alt, max_az, worst


def test_parity_against_typescript() -> None:
    assert _FIXTURES, "no fixtures loaded"
    max_alt, max_az, worst = _max_errors()
    assert max_alt < _TOL_DEG, f"altitude drift {max_alt} deg exceeds {_TOL_DEG}: {worst}"
    assert max_az < _TOL_DEG, f"azimuth drift {max_az} deg exceeds {_TOL_DEG}: {worst}"


def test_bounds() -> None:
    for fx in _FIXTURES:
        moment = datetime(fx["year"], fx["month"], fx["day"], fx["hour"], tzinfo=timezone.utc)
        got = sun_position(moment, fx["lat"], fx["lon"])
        assert -90.0 <= got.altitude <= 90.0
        assert 0.0 <= got.azimuth <= 360.0


def test_naive_datetime_treated_as_utc() -> None:
    aware = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)
    naive = datetime(2026, 6, 21, 12)
    assert sun_position(naive, 48.8566, 2.3522) == sun_position(aware, 48.8566, 2.3522)


if __name__ == "__main__":
    test_parity_against_typescript()
    test_bounds()
    test_naive_datetime_treated_as_utc()
    alt, az, _ = _max_errors()
    print(f"OK  {len(_FIXTURES)} samples")
    print(f"max altitude drift: {alt:.3e} deg")
    print(f"max azimuth  drift: {az:.3e} deg")
