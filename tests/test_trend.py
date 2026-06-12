"""Tests for the today-forecast-trend logic."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.trend import (  # noqa: E402
    TrendReference,
    compute_trend,
    should_capture,
)

_UTC = timezone.utc


def _ref(date: str, kwh: float) -> TrendReference:
    return TrendReference(date=date, kwh=kwh, captured_at=datetime(2026, 6, 12, 6, tzinfo=_UTC))


def test_should_capture_waits_for_anchor() -> None:
    before = datetime(2026, 6, 12, 5, tzinfo=_UTC)
    at = datetime(2026, 6, 12, 6, tzinfo=_UTC)
    after = datetime(2026, 6, 12, 9, tzinfo=_UTC)
    assert should_capture(None, "2026-06-12", before, 6) is False
    assert should_capture(None, "2026-06-12", at, 6) is True
    assert should_capture(None, "2026-06-12", after, 6) is True


def test_should_capture_once_per_day() -> None:
    now = datetime(2026, 6, 12, 9, tzinfo=_UTC)
    # Today's reference already taken: do not recapture.
    assert should_capture(_ref("2026-06-12", 20.0), "2026-06-12", now, 6) is False
    # Yesterday's reference is stale: capture today's.
    assert should_capture(_ref("2026-06-11", 20.0), "2026-06-12", now, 6) is True


def test_compute_trend_directions() -> None:
    ref = _ref("2026-06-12", 20.0)
    up = compute_trend(ref, 23.0, "2026-06-12")
    assert up.direction == "up" and abs(up.delta_kwh - 3.0) < 1e-9
    assert up.reference_kwh == 20.0 and up.current_kwh == 23.0

    down = compute_trend(ref, 18.0, "2026-06-12")
    assert down.direction == "down" and abs(down.delta_kwh + 2.0) < 1e-9

    flat = compute_trend(ref, 20.05, "2026-06-12")
    assert flat.direction == "flat"


def test_compute_trend_unknown_without_reference() -> None:
    none = compute_trend(None, 21.0, "2026-06-12")
    assert none.delta_kwh is None and none.direction == "unknown" and none.current_kwh == 21.0
    # Stale reference (different day) is also unknown until today's is taken.
    stale = compute_trend(_ref("2026-06-11", 20.0), 21.0, "2026-06-12")
    assert stale.delta_kwh is None and stale.direction == "unknown"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all trend tests passed")
