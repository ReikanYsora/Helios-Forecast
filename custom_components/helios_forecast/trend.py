"""Today's forecast trend.

How much today's predicted total energy has moved since a fixed daily reference,
the snapshot taken at a set local hour (default 06:00, when the day's outlook is
settled enough to act on). Positive means the day now looks better than at the
reference, negative means worse, and the magnitude is the kWh swing. The reference
is captured once per day and frozen; the trend is recomputed against it every
refresh, so a user with panels + battery can see, mid-day, whether the outlook is
improving or degrading versus this morning.

Pure functions, no Home Assistant; the coordinator owns the persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Below this absolute swing (kWh) the day is reported "flat" rather than up / down.
FLAT_KWH = 0.1


@dataclass(frozen=True)
class TrendReference:
    """The frozen daily reference: today's predicted total at capture time."""

    date: str              # local ISO date the reference belongs to
    kwh: float
    captured_at: datetime  # UTC


@dataclass(frozen=True)
class TodayTrend:
    """Today's outlook versus its reference. delta_kwh None before the reference is set."""

    delta_kwh: Optional[float]
    reference_kwh: Optional[float]
    reference_time: Optional[datetime]
    current_kwh: float
    direction: str         # "up" | "down" | "flat" | "unknown"


def should_capture(reference: Optional[TrendReference], today_date: str, now_local: datetime, anchor_hour: int) -> bool:
    """Capture the reference once per day, at the first refresh at or after the anchor hour."""
    if reference is not None and reference.date == today_date:
        return False
    return now_local.hour >= anchor_hour


def compute_trend(reference: Optional[TrendReference], current_kwh: float, today_date: str) -> TodayTrend:
    """Trend of today's predicted total against the frozen reference.

    Unknown until the reference is captured (before the anchor hour, or a stale
    reference from a previous day before today's is taken)."""
    if reference is None or reference.date != today_date:
        return TodayTrend(None, None, None, current_kwh, "unknown")
    delta = current_kwh - reference.kwh
    if delta > FLAT_KWH:
        direction = "up"
    elif delta < -FLAT_KWH:
        direction = "down"
    else:
        direction = "flat"
    return TodayTrend(delta, reference.kwh, reference.captured_at, current_kwh, direction)
