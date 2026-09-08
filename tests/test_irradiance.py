"""Parity test for the PV power port.

Checks custom_components/helios_forecast/solar/irradiance.compute_pv_power against
golden values from the card's computePvPower (see
tests/fixtures/_gen_pv_power.mjs), across every branch: night, horizontal,
shaded, tilted, supplied GHI / direct-diffuse / GTI, thermal, trackers. Same
formula, so the match must be to floating-point precision. Runnable with
``python3 tests/test_irradiance.py`` or under pytest.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.solar.irradiance import (  # noqa: E402
    PanelOrientation,
    PvContext,
    IAM_B0,
    SYSTEM_LOSS_FACTOR,
    _incidence_modifier,
    cell_temperature_c,
    compute_pv_power,
    snow_cover_factor,
)

_FIXTURES = json.loads((_REPO_ROOT / "tests" / "fixtures" / "pv_power.json").read_text())

_TOL = 1e-9


def _panel(spec: dict | None) -> PanelOrientation | None:
    if spec is None:
        return None
    return PanelOrientation(tilt_deg=spec["tiltDeg"], azimuth_deg=spec["azimuthDeg"], tracker=spec.get("tracker"))


def _ctx(spec: dict | None) -> PvContext | None:
    if spec is None:
        return None
    return PvContext(
        air_temp_c=spec.get("airTempC"),
        wind_ms=spec.get("windMs"),
        shading=bool(spec.get("shading", False)),
        ghi_wm2=spec.get("ghiWm2"),
        direct_wm2=spec.get("directWm2"),
        diffuse_wm2=spec.get("diffuseWm2"),
        poa_wm2=spec.get("poaWm2"),
    )


def test_wind_never_cools_a_panel_below_the_air_around_it() -> None:
    """Wind carries away the heat the sun put into the panel; it cannot take the panel below ambient.

    Subtracted from the air temperature instead, a windy overcast hour reports a cell colder than the
    air, and the thermal derate turns that into a production bonus exactly when there is least sun.
    """
    for wind_ms in (0.0, 5.0, 20.0):
        assert cell_temperature_c(15.0, 0.0, wind_ms) == 15.0
        assert cell_temperature_c(15.0, 50.0, wind_ms) >= 15.0
    # And with real sun the wind still cools, just not past the air.
    assert cell_temperature_c(15.0, 900.0, 8.0) < cell_temperature_c(15.0, 900.0, 0.0)


def test_parity_against_typescript() -> None:
    assert _FIXTURES, "no fixtures loaded"
    max_err = 0.0
    worst = None
    for fx in _FIXTURES:
        moment = datetime(fx["year"], fx["month"], fx["day"], fx["hour"], tzinfo=timezone.utc)
        got = compute_pv_power(moment, fx["lat"], fx["lon"], fx["cloud"], _panel(fx["panel"]), _ctx(fx["ctx"]))
        err = abs(got - fx["expected"])
        if err > max_err:
            max_err, worst = err, {"fixture": fx, "got": got}
    assert max_err < _TOL, f"PV power drift {max_err} exceeds {_TOL}: {worst}"


def test_bounds() -> None:
    for fx in _FIXTURES:
        moment = datetime(fx["year"], fx["month"], fx["day"], fx["hour"], tzinfo=timezone.utc)
        got = compute_pv_power(moment, fx["lat"], fx["lon"], fx["cloud"], _panel(fx["panel"]), _ctx(fx["ctx"]))
        assert 0.0 <= got <= 100.0


def test_snow_cover_factor() -> None:
    assert snow_cover_factor(None, 1.0) == 1.0  # no snow series
    assert snow_cover_factor(0.005, -5.0) == 1.0  # below 1 cm: a dusting, no cover
    assert snow_cover_factor(0.20, -5.0) == 0.1  # deep snow, freezing: fully covered (min factor)
    assert snow_cover_factor(0.20, None) == 0.1  # unknown air treated as cold
    assert snow_cover_factor(0.20, 4.0) == 1.0  # warm enough to have shed
    assert abs(snow_cover_factor(0.20, 2.0) - (0.1 + 0.9 * 0.5)) < 1e-12  # mid-melt ramp


if __name__ == "__main__":
    test_parity_against_typescript()
    test_bounds()
    test_snow_cover_factor()
    max_err = max(
        abs(
            compute_pv_power(
                datetime(fx["year"], fx["month"], fx["day"], fx["hour"], tzinfo=timezone.utc),
                fx["lat"],
                fx["lon"],
                fx["cloud"],
                _panel(fx["panel"]),
                _ctx(fx["ctx"]),
            )
            - fx["expected"]
        )
        for fx in _FIXTURES
    )
    print(f"OK  {len(_FIXTURES)} scenarios")
    print(f"max PV power drift: {max_err:.3e} %")


def test_the_system_loss_factor_is_the_pvwatts_default() -> None:
    """Everything between the plane of the array and the meter that the optics do not model.

    Pinned to a number that comes from outside this project: PVWatts' own defaults, 14 % system
    losses on a 96 % inverter. The fleet asked for 0.827, which is the same figure; changing it is a
    recalibration and has to come through here.
    """
    assert SYSTEM_LOSS_FACTOR == 0.825
    assert abs(SYSTEM_LOSS_FACTOR - 0.86 * 0.96) < 0.001


def test_the_incidence_modifier_lets_everything_through_head_on_and_nothing_edge_on() -> None:
    """Glass reflects more of the beam the further the sun is from the normal."""
    assert IAM_B0 == 0.05
    assert _incidence_modifier(1.0) == 1.0  # head-on, nothing lost
    assert _incidence_modifier(0.0) == 0.0  # edge-on, and no division by zero
    assert _incidence_modifier(-0.3) == 0.0  # behind the panel
    # 60 degrees off the normal: cos = 0.5, so 1 - 0.05 * (2 - 1).
    assert abs(_incidence_modifier(0.5) - 0.95) < 1e-12
    # It only ever takes away, and monotonically.
    values = [_incidence_modifier(c) for c in (1.0, 0.8, 0.6, 0.4, 0.2, 0.05)]
    assert values == sorted(values, reverse=True)
    assert all(0.0 <= v <= 1.0 for v in values)
