"""Tests for the 360-degree soft horizon shading physics engine."""

import math
from typing import List

from custom_components.helios_forecast.solar.horizon import (
    HORIZON_SECTORS,
    beam_shading_factor,
    default_horizon_profile,
    interpolate_horizon_elevation,
    parse_horizon_profile,
)


def test_default_horizon_profile() -> None:
    profile = default_horizon_profile()
    assert len(profile) == HORIZON_SECTORS
    assert all(h == 0.0 for h in profile)


def test_interpolate_horizon_elevation_boundaries() -> None:
    # 24 sectors, 15 deg per sector. Index 0 = 0 deg, index 6 = 90 deg, index 12 = 180 deg.
    profile = [0.0] * HORIZON_SECTORS
    profile[0] = 10.0   # 0 deg
    profile[6] = 20.0   # 90 deg
    profile[12] = 30.0  # 180 deg

    assert interpolate_horizon_elevation(0.0, profile) == 10.0
    assert interpolate_horizon_elevation(90.0, profile) == 20.0
    assert interpolate_horizon_elevation(180.0, profile) == 30.0
    # Interpolation mid-way between 0 deg (10) and 15 deg (0) -> 5.0 deg
    assert math.isclose(interpolate_horizon_elevation(7.5, profile), 5.0)


def test_interpolate_horizon_elevation_wrap() -> None:
    # Test circular boundary wrapping between sector 23 (345 deg) and sector 0 (0 deg).
    profile = [0.0] * HORIZON_SECTORS
    profile[0] = 10.0
    profile[23] = 20.0  # 345 deg

    # At 352.5 deg (midway between 345 and 360/0), should be 15.0
    assert math.isclose(interpolate_horizon_elevation(352.5, profile), 15.0)


def test_beam_shading_factor_zero_disruption() -> None:
    # Unshaded / default profile guarantees beam_shading_factor == 1.0 everywhere daytime.
    profile = default_horizon_profile()
    for alt in [1.0, 10.0, 45.0, 90.0]:
        for az in [0.0, 90.0, 180.0, 270.0]:
            assert beam_shading_factor(alt, az, profile) == 1.0

    # Nighttime sun position returns 0.0
    assert beam_shading_factor(0.0, 180.0, profile) == 0.0
    assert beam_shading_factor(-10.0, 180.0, profile) == 0.0


def test_beam_shading_factor_attenuation() -> None:
    # Set a 20 deg obstruction at 90 deg azimuth (sector 6).
    profile = [0.0] * HORIZON_SECTORS
    profile[6] = 20.0

    # Sun at azimuth 90 deg, altitude well above obstacle (40 deg) -> ~1.0
    factor_high = beam_shading_factor(40.0, 90.0, profile, width_deg=2.0)
    assert factor_high > 0.99

    # Sun at azimuth 90 deg, altitude exactly at obstacle (20 deg) -> 0.5 (center of sigmoid)
    factor_mid = beam_shading_factor(20.0, 90.0, profile, width_deg=2.0)
    assert math.isclose(factor_mid, 0.5)

    # Sun at azimuth 90 deg, altitude well below obstacle (5 deg) -> ~0.0
    factor_low = beam_shading_factor(5.0, 90.0, profile, width_deg=2.0)
    assert factor_low < 0.01


def test_parse_horizon_profile() -> None:
    # String input parsing
    s = "10, 20.5, 30"
    parsed = parse_horizon_profile(s)
    assert len(parsed) == HORIZON_SECTORS
    assert parsed[0] == 10.0
    assert parsed[1] == 20.5
    assert parsed[2] == 30.0
    assert all(h == 0.0 for h in parsed[3:])

    # Clamp bounds check [0, 90]
    s_invalid = "-10, 150, invalid, nan"
    parsed_invalid = parse_horizon_profile(s_invalid)
    assert parsed_invalid[0] == 0.0
    assert parsed_invalid[1] == 90.0

    # Truncation check (more than 24 entries)
    long_list = [5.0] * 30
    parsed_long = parse_horizon_profile(long_list)
    assert len(parsed_long) == HORIZON_SECTORS
    assert all(h == 5.0 for h in parsed_long)
