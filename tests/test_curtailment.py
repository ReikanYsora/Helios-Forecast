"""Tests for the curtailment flagging: which produced hours the learning must not read as low."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.curtailment import (  # noqa: E402
    CAP_FRACTION,
    FULL_SOC_PCT,
    flag_curtailed,
    on_intervals_from_states,
)
from custom_components.helios_forecast.solar.residual import ProductionBucket  # noqa: E402

_H = 3_600_000.0


def _bucket(hour: int, kwh: float) -> ProductionBucket:
    return ProductionBucket(start_ms=hour * _H, end_ms=(hour + 1) * _H, kwh=kwh)


def test_full_battery_at_the_cap_is_curtailed_but_either_alone_is_not() -> None:
    cap_w = 1200.0
    buckets = [_bucket(10, 1.15), _bucket(11, 1.15), _bucket(12, 0.6), _bucket(13, 1.15)]
    soc = {10 * _H: 100.0, 11 * _H: 97.0, 12 * _H: 100.0}  # hour 13: no reading
    out = flag_curtailed(buckets, soc_max_by_start_ms=soc, cap_w=cap_w)
    assert [b.curtailed for b in out] == [True, False, False, False]
    # Untouched buckets keep their identity; the flagged one is a copy with the same energy.
    assert out[1] is buckets[1]
    assert out[0].kwh == 1.15 and out[0].start_ms == buckets[0].start_ms


def test_battery_signal_needs_a_known_cap() -> None:
    buckets = [_bucket(10, 5.0)]
    soc = {10 * _H: 100.0}
    assert flag_curtailed(buckets, soc_max_by_start_ms=soc, cap_w=None)[0].curtailed is False
    assert flag_curtailed(buckets, soc_max_by_start_ms=soc, cap_w=float("inf"))[0].curtailed is False
    assert flag_curtailed(buckets, soc_max_by_start_ms=soc, cap_w=5000.0)[0].curtailed is True


def test_thresholds_sit_where_documented() -> None:
    cap_w = 1000.0
    at_cap = _bucket(10, CAP_FRACTION)  # exactly CAP_FRACTION x 1 kW over the hour
    below = _bucket(11, CAP_FRACTION - 0.01)
    soc = {10 * _H: FULL_SOC_PCT, 11 * _H: FULL_SOC_PCT}
    out = flag_curtailed([at_cap, below], soc_max_by_start_ms=soc, cap_w=cap_w)
    assert out[0].curtailed is True
    assert out[1].curtailed is False


def test_curtailment_entity_marks_hours_it_was_on_for_at_least_half() -> None:
    buckets = [_bucket(h, 0.5) for h in range(10, 14)]
    # On from 10:40 to 12:20: hour 10 is on for 20 min (no), hour 11 fully (yes), hour 12 for 20 min (no).
    on = [(10 * _H + 40 * 60_000, 12 * _H + 20 * 60_000)]
    out = flag_curtailed(buckets, on_intervals=on)
    assert [b.curtailed for b in out] == [False, True, False, False]
    # Exactly half counts.
    half = [(13 * _H, 13 * _H + 30 * 60_000)]
    assert flag_curtailed(buckets, on_intervals=half)[3].curtailed is True


def test_on_intervals_from_states_clips_and_closes() -> None:
    start, end = 0.0, 4 * _H
    states = [(-_H, "on"), (1 * _H, "off"), (2 * _H, "on"), (2.5 * _H, "unavailable"), (3 * _H, "on")]
    assert on_intervals_from_states(states, start, end) == [(0.0, 1 * _H), (2 * _H, 2.5 * _H), (3 * _H, 4 * _H)]
    assert on_intervals_from_states([], start, end) == []
    assert on_intervals_from_states([(-_H, "off")], start, end) == []
