"""Learned sky-residual correction, port of the card's forecast-sky.ts.

Per (sun-azimuth, sun-altitude) cell, learns the ratio between what the home
actually produced and what the model predicted, over a rolling 60-day window,
recency-weighted. At forecast time the ratio replaces a flat scalar: forecast =
model x sky-ratio(sun position). A thin cell leans on the global mean ratio; too
little history returns None so the caller keeps the uncorrected forecast.

The learning deliberately keeps every produced hour, including hours where a
hybrid inverter curtailed output once the battery was full: that curtailment is
part of what the home really harvests, so learning it pulls the forecast toward
the realistic (curtailed) production instead of the theoretical potential.

Pure, no Home Assistant imports. The map is stored as plain float lists (the card
uses Float32Array; the values agree to single-precision).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .gti import GtiStore, sample_gti
from .irradiance import snow_cover_factor
from .geometry import sun_position
from .power import PvLayout, WeatherSample, compute_pv_power_weighted

# Learning window and cell grid.
LEARN_DAYS = 60
AZ_STEP_DEG = 10
ALT_STEP_DEG = 5
N_AZ = round(360 / AZ_STEP_DEG)   # 36
N_ALT = round(90 / ALT_STEP_DEG)  # 18

RECENCY_HALF_LIFE_MS = 30 * 24 * 3_600_000
CONF_W0 = 4
M_MIN = 0.2
M_MAX = 2.5
MIN_TOTAL_WEIGHT = 3
MODEL_KWH_FLOOR = 0.05
LEARN_SUBSAMPLES = 4
SMOOTH_CONF_KEEP = 0.6
SMOOTH_NEIGHBOR_W = 0.6


@dataclass(frozen=True)
class ProductionBucket:
    """Hourly produced energy (recorder `change`)."""

    start_ms: float
    end_ms: float
    kwh: float


@dataclass(frozen=True)
class SkyResidualMap:
    """Learned actual/model ratio + confidence per sky cell."""

    n_az: int
    n_alt: int
    m: List[float]
    conf: List[float]
    global_ratio: float
    total_weight: float
    visited_cells: int


@dataclass(frozen=True)
class SkyResidualInput:
    """Decoupled inputs so the build stays a pure function."""

    lat: float
    lon: float
    layout: PvLayout
    production: Optional[List[ProductionBucket]]
    cloud_times: List[float]   # epoch ms, ascending
    cloud: List[float]
    shortwave: List[float]
    direct: List[float]
    diffuse: List[float]
    temp: List[float]
    wind: List[float]
    snow: List[float]
    gti_store: Optional[GtiStore]
    now_ms: float


def _dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def sample_sky_residual(sky_map: SkyResidualMap, az_deg: float, alt_deg: float) -> float:
    """Bilinear actual/model ratio at a sun position, confidence-blended."""
    g = sky_map.global_ratio
    if alt_deg <= 0:
        return g
    az = az_deg % 360
    alt = max(0.0, min(90 - 1e-3, alt_deg))

    f_az = az / AZ_STEP_DEG
    f_alt = alt / ALT_STEP_DEG
    az0 = math.floor(f_az)
    alt0 = math.floor(f_alt)
    d_az = f_az - az0
    d_alt = f_alt - alt0

    num = 0.0
    den = 0.0
    for i in (0, 1):
        for j in (0, 1):
            ai = (az0 + i) % sky_map.n_az
            aj = alt0 + j
            if aj < 0 or aj >= sky_map.n_alt:
                continue
            idx = aj * sky_map.n_az + ai
            cell_m = sky_map.conf[idx] * sky_map.m[idx] + (1 - sky_map.conf[idx]) * g
            w = (1 - d_az if i == 0 else d_az) * (1 - d_alt if j == 0 else d_alt)
            num += w * cell_m
            den += w
    return num / den if den > 0 else g


def build_sky_residual_map(inp: SkyResidualInput) -> Optional[SkyResidualMap]:
    """Build the residual map from the production + weather history, or None."""
    k = inp.layout.total_kwp * 10.0
    if k <= 0:
        return None
    if not inp.production:
        return None
    if not inp.cloud_times:
        return None

    sum_w = [0.0] * (N_AZ * N_ALT)
    sum_wr = [0.0] * (N_AZ * N_ALT)
    global_sum_w = 0.0
    global_sum_wr = 0.0

    sampler = (lambda tilt, az, m: sample_gti(inp.gti_store, tilt, az, m)) if inp.gti_store else None

    for bucket in inp.production:
        kwh = bucket.kwh
        if not math.isfinite(kwh) or kwh < 0:
            continue
        mid = (bucket.start_ms + bucket.end_ms) / 2
        sun = sun_position(_dt(mid), inp.lat, inp.lon)
        if sun.altitude <= 0:
            continue

        ci = _nearest_cloud_idx(inp.cloud_times, mid)
        cloud = _clamp_pct(inp.cloud[ci]) if ci >= 0 else 0.0
        ghi = inp.shortwave[ci] if ci >= 0 else None
        direct = inp.direct[ci] if ci >= 0 else None
        diffuse = inp.diffuse[ci] if ci >= 0 else None
        temp = inp.temp[ci] if ci >= 0 else None
        wind = inp.wind[ci] if ci >= 0 else None
        snow = inp.snow[ci] if ci >= 0 else None

        sample = WeatherSample(cloud=cloud, ghi=ghi, direct=direct, diffuse=diffuse, temp=temp, wind=wind)

        w_sum = 0.0
        w_n = 0
        for s in range(LEARN_SUBSAMPLES):
            sub_t = bucket.start_ms + (s + 0.5) * (bucket.end_ms - bucket.start_ms) / LEARN_SUBSAMPLES
            moment = _dt(sub_t)
            if sun_position(moment, inp.lat, inp.lon).altitude <= 0:
                continue
            w_sum += compute_pv_power_weighted(moment, inp.lat, inp.lon, sample, inp.layout, sampler)
            w_n += 1
        if w_n == 0:
            continue
        model_kwh = (w_sum / w_n) * k * snow_cover_factor(snow, temp) / 1000.0
        if model_kwh < MODEL_KWH_FLOOR:
            continue

        ratio = kwh / model_kwh
        if not math.isfinite(ratio) or ratio < 0:
            continue

        age_ms = max(0.0, inp.now_ms - mid)
        recency = math.pow(0.5, age_ms / RECENCY_HALF_LIFE_MS)
        clear = max(0.1, 1 - cloud / 100)
        w = recency * clear
        if w <= 0:
            continue

        az_idx = min(N_AZ - 1, max(0, math.floor(((sun.azimuth % 360 + 360) % 360) / AZ_STEP_DEG)))
        alt_idx = min(N_ALT - 1, max(0, math.floor(sun.altitude / ALT_STEP_DEG)))
        idx = alt_idx * N_AZ + az_idx

        sum_w[idx] += w
        sum_wr[idx] += w * ratio
        global_sum_w += w
        global_sum_wr += w * ratio

    if global_sum_w < MIN_TOTAL_WEIGHT:
        return None
    global_ratio = max(M_MIN, min(M_MAX, global_sum_wr / global_sum_w))
    if not (global_ratio > 0):
        return None

    m = [global_ratio] * (N_AZ * N_ALT)
    conf = [0.0] * (N_AZ * N_ALT)
    visited = 0
    for i in range(len(m)):
        if sum_w[i] <= 0:
            continue
        m[i] = max(M_MIN, min(M_MAX, sum_wr[i] / sum_w[i]))
        conf[i] = 1 - math.exp(-sum_w[i] / CONF_W0)
        visited += 1

    m_s = list(m)
    conf_s = list(conf)
    for aj in range(N_ALT):
        for ai in range(N_AZ):
            idx = aj * N_AZ + ai
            self_conf = conf[idx]
            if self_conf >= SMOOTH_CONF_KEEP:
                continue
            num = 0.0
            den = 0.0
            best_nb_conf = 0.0
            for d_alt in (-1, 0, 1):
                nj = aj + d_alt
                if nj < 0 or nj >= N_ALT:
                    continue
                for d_az in (-1, 0, 1):
                    if d_alt == 0 and d_az == 0:
                        continue
                    ni = (ai + d_az + N_AZ) % N_AZ
                    n_idx = nj * N_AZ + ni
                    c = conf[n_idx]
                    num += c * m[n_idx]
                    den += c
                    if c > best_nb_conf:
                        best_nb_conf = c
            if den <= 0 or best_nb_conf <= 0:
                continue
            nb_m = num / den
            nb_pull = SMOOTH_NEIGHBOR_W * best_nb_conf
            m_s[idx] = (self_conf * m[idx] + nb_pull * nb_m) / (self_conf + nb_pull)
            conf_s[idx] = min(1.0, self_conf + nb_pull)

    return SkyResidualMap(
        n_az=N_AZ, n_alt=N_ALT, m=m_s, conf=conf_s,
        global_ratio=global_ratio, total_weight=global_sum_w, visited_cells=visited,
    )


def _nearest_cloud_idx(times: List[float], t_ms: float) -> int:
    """Index of the nearest sample by time, with the card's early break."""
    if not times:
        return -1
    best = 0
    best_dt = math.inf
    for i in range(len(times)):
        dt = abs(times[i] - t_ms)
        if dt < best_dt:
            best_dt = dt
            best = i
        elif times[i] > t_ms and dt > best_dt:
            break
    return best


def _clamp_pct(v: float) -> float:
    return max(0.0, min(100.0, v)) if math.isfinite(v) else 0.0
