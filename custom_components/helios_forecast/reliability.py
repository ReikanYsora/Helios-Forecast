"""Forecast reliability index.

A 0..100 confidence score blending three measurable signals:

  - data maturity: how many days of production history back the learning (a fresh
    install corrects nothing; ~60 days is fully warmed up).
  - recent skill: how close the day-ahead prediction, recorded before the day
    happened, came to what the meter then measured over the last couple of weeks.
  - today's predictability: a clear or steadily overcast sky is highly
    predictable; broken, variable cloud is intrinsically uncertain whatever the
    data, so we read the spread of today's daytime cloud forecast.

A signal that cannot be measured contributes nothing rather than being shared out
among the others, so the index is a floor: this much could be established.

Pure functions, no Home Assistant, so the blend can be unit-tested on its own.
The inputs are duck-typed: production buckets expose ``.start_ms`` + ``.kwh``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Dict, List, Optional

# Days of history at which the learning is considered fully matured.
MATURITY_TARGET_DAYS = 60
# Trailing window for the recent-skill comparison.
SKILL_WINDOW_DAYS = 14
# Minimum real daily energy (kWh) for a day to count in the skill comparison, so
# a fully overcast or sensor-down day does not dominate the relative error.
SKILL_MIN_DAY_KWH = 0.5
# Daytime gate for the predictability read: hours whose modelled GHI clears this
# (W/m2) count as daytime, so night zeros do not flatten the cloud spread.
DAYTIME_GHI_WM2 = 20.0

_W_MATURITY = 0.35
_W_SKILL = 0.45
_W_PREDICT = 0.20


@dataclass(frozen=True)
class Reliability:
    """The reliability index and its components."""

    overall: float  # 0..100
    data_maturity: float  # 0..1
    recent_skill: Optional[float]  # 0..1, None when too few comparable days
    today_predictability: Optional[float]  # 0..1, None when today has no daytime data
    days_learned: int
    per_day: List[float]  # 0..100 per horizon day (today .. +6)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _finite(v: object) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


def _local_date(ms: float, tz: tzinfo):
    return datetime.fromtimestamp(ms / 1000.0, tz).date()


def daily_actual_kwh(production: list, tz: tzinfo) -> Dict[date, float]:
    """Real production summed to kWh per local calendar day."""
    out: Dict[date, float] = {}
    for b in production:
        if not _finite(getattr(b, "kwh", None)):
            continue
        day = _local_date(b.start_ms, tz)
        out[day] = out.get(day, 0.0) + max(0.0, b.kwh)
    return out


def data_maturity(production: list, tz: tzinfo) -> tuple[float, int]:
    """Fraction in [0, 1] of the maturity target, plus the distinct-day count."""
    days = len({_local_date(b.start_ms, tz) for b in production if _finite(getattr(b, "kwh", None))})
    return _clamp01(days / MATURITY_TARGET_DAYS), days


def recent_skill(predicted: Dict[date, float], production: list, now: datetime, tz: tzinfo) -> Optional[float]:
    """1 - mean relative daily error over the trailing window, or None when too few comparable days.

    ``predicted`` holds what was forecast for each day before that day happened, kept by the
    coordinator. Reconstructing it instead from the archive would measure nothing: the archive is
    rebuilt every hour by the current model, whose residual map and analog library are fitted on the
    very production being compared against, so the answer comes out near perfect whatever the real
    skill. Today is excluded, still in progress.
    """
    actual = daily_actual_kwh(production, tz)
    today = now.astimezone(tz).date()
    errs: List[float] = []
    for day, act in actual.items():
        if day >= today:
            continue
        if (today - day).days > SKILL_WINDOW_DAYS:
            continue
        if act < SKILL_MIN_DAY_KWH:
            continue
        pred = predicted.get(day)
        if pred is None:
            continue
        errs.append(_clamp01(abs(pred - act) / act))
    if len(errs) < 2:
        return None
    return _clamp01(1.0 - sum(errs) / len(errs))


def _daytime_cloud_for_day(weather, day, tz: tzinfo) -> List[float]:
    clouds: List[float] = []
    times = weather.times
    cloud = weather.cloud
    ghi = weather.shortwave
    for i, t in enumerate(times):
        if t.astimezone(tz).date() != day:
            continue
        g = ghi[i] if i < len(ghi) else None
        if not isinstance(g, (int, float)) or not math.isfinite(g) or g <= DAYTIME_GHI_WM2:
            continue
        c = cloud[i] if i < len(cloud) else None
        if isinstance(c, (int, float)) and math.isfinite(c):
            clouds.append(float(c))
    return clouds


def _predictability_from_clouds(clouds: List[float]) -> Optional[float]:
    """High when daytime cloud is steady (clear or solid overcast), low when it is
    broken and variable. Reads the standard deviation of the daytime cloud cover."""
    if len(clouds) < 3:
        return None
    mean = sum(clouds) / len(clouds)
    sd = (sum((c - mean) ** 2 for c in clouds) / len(clouds)) ** 0.5
    return _clamp01(1.0 - sd / 40.0)


def _daytime_spread_for_day(weather, day, tz: tzinfo) -> Optional[float]:
    """Mean cross-model cloud disagreement over the day's daytime hours, or None when
    the model ensemble carries no spread (single-model response / test fixtures)."""
    spreads: List[float] = []
    times = weather.times
    ghi = weather.shortwave
    sp = getattr(weather, "cloud_spread", []) or []
    for i, t in enumerate(times):
        if t.astimezone(tz).date() != day:
            continue
        g = ghi[i] if i < len(ghi) else None
        if not isinstance(g, (int, float)) or not math.isfinite(g) or g <= DAYTIME_GHI_WM2:
            continue
        s = sp[i] if i < len(sp) else None
        if isinstance(s, (int, float)) and math.isfinite(s):
            spreads.append(float(s))
    if not spreads:
        return None
    return sum(spreads) / len(spreads)


def _day_predictability(weather, day, tz: tzinfo) -> Optional[float]:
    """Predictability for one day: the steadiness of the cloud forecast (low temporal
    spread) combined with model agreement (low cross-model spread). Averages whichever
    of the two signals are available."""
    p_var = _predictability_from_clouds(_daytime_cloud_for_day(weather, day, tz))
    spread = _daytime_spread_for_day(weather, day, tz)
    p_spread = None if spread is None else _clamp01(1.0 - spread / 30.0)
    parts = [v for v in (p_var, p_spread) if v is not None]
    if not parts:
        return None
    return sum(parts) / len(parts)


def today_predictability(weather, now: datetime, tz: tzinfo) -> Optional[float]:
    return _day_predictability(weather, now.astimezone(tz).date(), tz)


def _horizon_decay(day_index: int) -> float:
    """Cloud-forecast skill drops with lead time; reliability decays accordingly. Exponential toward a
    0.5 floor (J0=1.00, J1=0.88, J3=0.71, J6=0.58), matching how NWP skill actually degrades."""
    return 0.5 + 0.5 * math.exp(-day_index / 3.5)


def _blend(maturity: float, skill: Optional[float], predict: Optional[float]) -> float:
    """The three signals against the full weight, so a signal that could not be measured contributes
    nothing rather than being shared out among the others.

    Renormalising over the survivors made the index rise when a signal went missing, and the one that
    goes missing is recent skill: it needs two comparable days in the trailing fortnight, which a
    northern winter or a run of overcast does not always give. The index then climbed precisely when
    there was least ground to trust it. What it says now is a floor: this much could be established.
    """
    parts = [(maturity, _W_MATURITY), (skill, _W_SKILL), (predict, _W_PREDICT)]
    return 100.0 * sum(v * w for v, w in parts if v is not None) / (_W_MATURITY + _W_SKILL + _W_PREDICT)


def compute_reliability(
    production: list, predicted: Dict[date, float], weather, now: datetime, tz: tzinfo
) -> Reliability:
    """Blend the three signals into the overall index plus a per-horizon-day list."""
    maturity, days = data_maturity(production, tz)
    skill = recent_skill(predicted, production, now, tz)
    predict = today_predictability(weather, now, tz)

    overall = _blend(maturity, skill, predict)

    today = now.astimezone(tz).date()
    per_day: List[float] = []
    for n in range(7):
        day = today + timedelta(days=n)
        # n=0 is today: reuse `predict`, already computed above, instead of recomputing it.
        day_predict = predict if n == 0 else _day_predictability(weather, day, tz)
        base = _blend(maturity, skill, day_predict)
        per_day.append(round(base * _horizon_decay(n), 1))

    return Reliability(
        overall=round(overall, 1),
        data_maturity=round(maturity, 3),
        recent_skill=None if skill is None else round(skill, 3),
        today_predictability=None if predict is None else round(predict, 3),
        days_learned=days,
        per_day=per_day,
    )
