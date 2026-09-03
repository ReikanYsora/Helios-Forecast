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
    days = [DayForecast(date="2026-06-21", energy_kwh=12.3, peak_power_w=1000.0, peak_time=now, energy_raw_kwh=13.5)]
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
    assert response["result"]["daily"] == [{"date": "2026-06-21", "kwh": 12.3, "kwh_raw": 13.5}]


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


async def test_ws_series_uses_archive_up_to_its_own_last_point_then_live(hass, hass_ws_client) -> None:
    # Archive reaches well past midnight into today (14:00) - its own last point, not the
    # live series' start, is the switch-over instant. This matters because enrich_points()
    # deliberately leaves the live series' already-elapsed points unclamped (#52): if the
    # split still happened at midnight, today's own elapsed hours would come from those
    # raw live points instead of the archive's analog-clamped ones.
    midnight = datetime(2026, 6, 21, 0, tzinfo=_UTC)
    archive_end = midnight + timedelta(hours=14)
    archive = [
        ForecastPoint(t=midnight - timedelta(hours=1), pv_w=1.0, pv_raw_w=1.0),
        ForecastPoint(t=midnight, pv_w=2.0, pv_raw_w=2.0),
        ForecastPoint(t=archive_end, pv_w=50.0, pv_raw_w=50.0),  # today, 14:00 - the corrected value
    ]
    live = [
        # Same instant as the archive's last point: the archive's corrected value wins,
        # this raw one must be dropped, not just deduplicated arbitrarily.
        ForecastPoint(t=archive_end, pv_w=999.0, pv_raw_w=999.0),
        ForecastPoint(t=archive_end + timedelta(minutes=15), pv_w=4.0, pv_raw_w=4.0),
    ]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=live, archive_points=archive)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    watts = [p["pv_w"] for p in response["result"]["points"]]
    assert watts == [1.0, 2.0, 50.0, 4.0]


async def test_ws_series_empty_archive_falls_back_to_live_entirely(hass, hass_ws_client) -> None:
    live = [ForecastPoint(t=datetime(2026, 6, 21, 0, tzinfo=_UTC), pv_w=5.0, pv_raw_w=5.0)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=live, archive_points=[])}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    assert [p["pv_w"] for p in response["result"]["points"]] == [5.0]


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


async def test_ws_series_daily_kwh_raw_is_the_pre_correction_total(hass, hass_ws_client) -> None:
    # kwh and kwh_raw must read from their own independently-summed daily totals (energy_kwh
    # from pv_w, energy_raw_kwh from pv_raw_w), not collapse onto the same figure.
    now = datetime(2026, 6, 21, tzinfo=_UTC)
    days = [DayForecast(date="2026-06-21", energy_kwh=21.4, peak_power_w=1000.0, peak_time=now, energy_raw_kwh=22.9)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(days=days)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    daily = response["result"]["daily"][0]
    assert daily["kwh"] == 21.4
    assert daily["kwh_raw"] == 22.9
    assert daily["kwh"] != daily["kwh_raw"]


async def test_ws_series_resolution_min_resamples_into_averaged_buckets(hass, hass_ws_client) -> None:
    base = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    points = [
        ForecastPoint(t=base, pv_w=100.0, pv_raw_w=90.0, pv_p10=80.0, pv_p90=120.0),
        ForecastPoint(t=base + timedelta(minutes=15), pv_w=200.0, pv_raw_w=180.0, pv_p10=None, pv_p90=None),
        ForecastPoint(t=base + timedelta(minutes=30), pv_w=300.0, pv_raw_w=270.0, pv_p10=250.0, pv_p90=350.0),
        ForecastPoint(t=base + timedelta(minutes=45), pv_w=400.0, pv_raw_w=360.0, pv_p10=350.0, pv_p90=450.0),
    ]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=points)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1", "resolution_min": 30})
    response = await client.receive_json()
    result_points = response["result"]["points"]
    assert len(result_points) == 2
    first, second = result_points
    assert first["t"] == base.isoformat()
    assert first["pv_w"] == 150.0  # avg(100, 200)
    assert first["pv_raw_w"] == 135.0  # avg(90, 180)
    assert first["pv_p10"] == 80.0  # None excluded, only the 10:00 bucket has a band
    assert first["pv_p90"] == 120.0
    assert second["t"] == (base + timedelta(minutes=30)).isoformat()
    assert second["pv_w"] == 350.0  # avg(300, 400)
    assert second["pv_raw_w"] == 315.0  # avg(270, 360)
    assert second["pv_p10"] == 300.0  # avg(250, 350)
    assert second["pv_p90"] == 400.0  # avg(350, 450)


async def test_ws_series_resolution_min_finer_than_native_leaves_points_unchanged(hass, hass_ws_client) -> None:
    # resolution_min at or below the native step is a no-op: native-resolution points, as-is.
    base = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    points = [
        ForecastPoint(t=base, pv_w=100.0, pv_raw_w=90.0),
        ForecastPoint(t=base + timedelta(minutes=15), pv_w=200.0, pv_raw_w=180.0),
    ]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=points)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1", "resolution_min": 15})
    response = await client.receive_json()
    assert [p["pv_w"] for p in response["result"]["points"]] == [100.0, 200.0]


async def test_ws_series_omitting_resolution_min_keeps_native_output_unchanged(hass, hass_ws_client) -> None:
    # Regression guard: the Helios card never sends resolution_min, so this path must stay
    # byte-for-byte identical to native-resolution output.
    base = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    points = [
        ForecastPoint(
            t=base + timedelta(minutes=5 * i),
            pv_w=float(i),
            pv_raw_w=float(i) * 0.9,
            pv_p10=float(i) - 1,
            pv_p90=float(i) + 1,
            ghi=500.0,
            cloud=10.0,
        )
        for i in range(6)
    ]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=points)}
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "helios_forecast/series", "entry_id": "entry1"})
    response = await client.receive_json()
    assert response["result"]["points"] == [
        {
            "t": p.t.isoformat(),
            "pv_w": p.pv_w,
            "pv_raw_w": p.pv_raw_w,
            "pv_p10": p.pv_p10,
            "pv_p90": p.pv_p90,
            "ghi": p.ghi,
            "cloud": p.cloud,
        }
        for p in points
    ]


async def test_ws_series_naive_start_returns_clean_error_not_a_crash(hass, hass_ws_client) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    points = [ForecastPoint(t=now, pv_w=1.0, pv_raw_w=1.0)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=points)}
    client = await hass_ws_client(hass)

    await client.send_json(
        {
            "id": 1,
            "type": "helios_forecast/series",
            "entry_id": "entry1",
            "start": "2026-06-21T10:00:00",  # no UTC offset
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_format"
