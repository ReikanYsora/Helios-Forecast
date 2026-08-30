"""Tests for the `helios_forecast/series` websocket command, the card's detail-series API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.helios_forecast import websocket as ws_mod
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.forecast import ForecastPoint
from custom_components.helios_forecast.summary import DayForecast

_UTC = timezone.utc


def _coordinator_with(points=None, archive_points=None, days=None):
    """A minimal stand-in exposing only what ws_series reads: .data.points, .archive_points,
    .data.summary.days."""
    summary = SimpleNamespace(days=days or [])
    data = SimpleNamespace(points=points or [], summary=summary)
    return SimpleNamespace(data=data, archive_points=archive_points or [])


@pytest.fixture(autouse=True)
def _register(hass):
    ws_mod.async_register(hass)


async def test_ws_series_returns_points_with_full_shape(hass, hass_ws_client) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    points = [ForecastPoint(t=now, pv_w=100.0, pv_raw_w=90.0, pv_p10=80.0, pv_p90=120.0, ghi=300.0, cloud=50.0)]
    days = [DayForecast(date="2026-06-21", energy_kwh=12.3, peak_power_w=1000.0, peak_time=now)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=points, days=days)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["points"] == [
        {
            "t": now.isoformat(),
            "pv_w": 100.0,
            "pv_raw_w": 90.0,
            "pv_p10": 80.0,
            "pv_p90": 120.0,
            "ghi": 300.0,
            "cloud": 50.0,
        }
    ]
    assert response["result"]["daily"] == [{"date": "2026-06-21", "kwh": 12.3, "kwh_raw": 12.3}]


async def test_ws_series_missing_ghi_cloud_default_to_none(hass, hass_ws_client) -> None:
    # A ForecastPoint built before ghi/cloud existed on the dataclass still has getattr defaults.
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    points = [ForecastPoint(t=now, pv_w=10.0, pv_raw_w=9.0)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=points)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    point = response["result"]["points"][0]
    assert point["ghi"] is None
    assert point["cloud"] is None


async def test_ws_series_filters_by_start_and_end(hass, hass_ws_client) -> None:
    base = datetime(2026, 6, 21, 0, tzinfo=_UTC)
    points = [ForecastPoint(t=base + timedelta(hours=h), pv_w=float(h), pv_raw_w=float(h)) for h in range(5)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=points)}
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "helios_forecast/series",
            "entry_id": "entry1",
            "start": (base + timedelta(hours=1)).isoformat(),
            "end": (base + timedelta(hours=3)).isoformat(),
        }
    )
    response = await client.receive_json()
    # [start, end): hours 1 and 2 only.
    assert [p["pv_w"] for p in response["result"]["points"]] == [1.0, 2.0]


async def test_ws_series_uses_archive_only_before_live_start(hass, hass_ws_client) -> None:
    live_start = datetime(2026, 6, 21, 0, tzinfo=_UTC)
    archive = [
        ForecastPoint(t=live_start - timedelta(hours=2), pv_w=1.0, pv_raw_w=1.0),
        ForecastPoint(t=live_start - timedelta(hours=1), pv_w=2.0, pv_raw_w=2.0),
        # An archive point landing on/after live_start must be dropped: the live series
        # already covers that instant at higher resolution.
        ForecastPoint(t=live_start, pv_w=999.0, pv_raw_w=999.0),
    ]
    live = [ForecastPoint(t=live_start, pv_w=3.0, pv_raw_w=3.0)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=live, archive_points=archive)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    watts = [p["pv_w"] for p in response["result"]["points"]]
    assert watts == [1.0, 2.0, 3.0]


async def test_ws_series_not_found_when_entry_id_unknown(hass, hass_ws_client) -> None:
    hass.data[DOMAIN] = {}
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "missing"})
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_ws_series_not_found_when_coordinator_never_refreshed(hass, hass_ws_client) -> None:
    coordinator = SimpleNamespace(data=None, archive_points=[])
    hass.data[DOMAIN] = {"entry1": coordinator}
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_ws_series_daily_kwh_raw_currently_mirrors_kwh(hass, hass_ws_client) -> None:
    # Characterizes the CURRENT behavior: `kwh_raw` is set from the same (residual-corrected)
    # `energy_kwh` as `kwh`, not from a true pre-correction sum (CONTRACT.md documents them as
    # distinct, e.g. kwh: 21.4 / kwh_raw: 22.9). Flagged separately as a contract mismatch; this
    # test only pins today's actual output so a fix is a deliberate, visible change here.
    now = datetime(2026, 6, 21, tzinfo=_UTC)
    days = [DayForecast(date="2026-06-21", energy_kwh=21.4, peak_power_w=1000.0, peak_time=now)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(days=days)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    daily = response["result"]["daily"][0]
    assert daily["kwh"] == daily["kwh_raw"] == 21.4
