# Data contract , Helios Solar Forecast

> Status: FROZEN (2026-06-11). This is the agreed interface between the
> integration and the Helios card. The model is ported against this schema;
> any change from here is a deliberate revision, not a moving target.

The integration owns one **config entry per PV system** (usually one). Every
surface below is scoped to that entry.

---

## How the card consumes the forecast , optional, two layers

The card no longer computes the forecast. The forecast is **entirely optional**:
with none configured in Home Assistant the card simply shows **no forecast curve
and no forecast label**, and everything else (map, sun, weather, live PV /
battery / grid chips, past curves) works exactly as before. No error, no nag.

When a forecast IS configured, the card reads it from Home Assistant in two
layers, so users can run it with whatever forecast they like:

- **Baseline (any provider).** The card reads Home Assistant's standard
  solar-production-forecast surface, the same hourly `wh_hours` the official
  Energy dashboard reads from its configured solar forecast sources. This works
  with **any** provider the user picked: Forecast.Solar, Solcast, or
  Helios-Forecast. It is what makes "use the card with the forecast of your
  choice" true.
- **Enhanced detail (Helios-Forecast only).** When this integration is installed
  and selected, the card additionally reads its richer series (sub-hourly, raw
  vs corrected band) for full-fidelity visuals. Falls back to the baseline curve
  when absent.

The card keeps its own weather (cloud overlay, sun-arc colouring, irradiance
chip) on its existing Open-Meteo path. Only the forecast moves out.

---

## 1. Energy dashboard provider (native HA) , what Helios-Forecast exposes

The integration ships an `energy.py` platform exposing:

```python
async def async_get_solar_forecast(hass, config_entry_id) -> dict:
    return {"wh_hours": {"2026-06-11T12:00:00+00:00": 3120, ...}}
```

- The user selects this integration's entry as the solar forecast source in the
  Energy dashboard's solar settings.
- `wh_hours` is hourly predicted production in **watt-hours**, residual-corrected,
  keyed by UTC hour. Horizon: the full 7-day compute; the Energy dashboard renders
  the near-term (today / tomorrow) it needs.
- This is the surface that lands the forecast in the **official Energy dashboard**,
  AND the surface the card's baseline layer reads (provider-agnostic: it is HA's
  own, not a Helios-only entity).

## 2. Sensors , first-class output, for automations

A proper, recorder-friendly entity set, grouped under one device per config
entry. This is a primary deliverable, not a side effect: the integration is
useful **on its own** (automations, history graphs, the Energy dashboard) with
or without the Helios card, and exposing entities for automations is a very
frequently requested feature. The set mirrors what Forecast.Solar / Solcast
users expect, so people switch over without relearning, and **every value is
residual-corrected**, so it tracks the site's real behaviour better than a raw
model. The card does not depend on these entity names for its baseline layer.

Days are numbered uniformly, **`day_1` = today** through **`day_7` = J+6**.

Power , now / next hour:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_power_now` | predicted PV power now, **W** | `device_class: power`, `state_class: measurement` |
| `sensor.helios_forecast_power_next_hour` | predicted average power over the next hour, **W** | |

Peak , per day over the 7-day horizon:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_peak_power_day_1` … `_day_7` | predicted peak power for day N, **W** | one entity per day, day 1 = today |
| `sensor.helios_forecast_peak_time_day_1` … `_day_7` | clock time of day N's peak | `device_class: timestamp`, one per day |

Energy , daily totals over the 7-day horizon:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_energy_day_1` … `_day_7` | predicted daily total, **kWh** | `device_class: energy`, `state_class: total`, one per day |
| `sensor.helios_forecast_energy_today_remaining` | predicted production left today, **kWh** | the one exception to day numbering, "remaining" only applies to today; drives "run the dishwasher if enough sun left" automations |

Energy , intraday:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_energy_this_hour` | predicted production this hour, **kWh** | |
| `sensor.helios_forecast_energy_next_hour` | predicted production next hour, **kWh** | |

All values are residual-corrected. A raw (pre-correction) variant is not exposed
as entities to keep the set clean; the raw curve stays available to the card via
the detail series in §3.

**Horizon.** The integration computes a 7-day forecast and every per-day entity
(energy AND peak) covers `day_1` (today) through `day_7` (J+6). Days beyond ~J+2
are inherently low-confidence for solar (cloud predictability collapses), to be
stated plainly in the docs. The Helios card's visible window is unchanged: **J-2
to J+2**, exactly as today; the extra forecast days live only in the entities.

## 3. Enhanced detail series (Helios-Forecast only) , WebSocket API

The premium layer. A dense, raw + corrected curve, more than belongs in a state
attribute. Travels over a WebSocket command, the same mechanism the card already
uses to pull recorder statistics. The card uses it only when present, otherwise
it stays on the baseline `wh_hours` curve.

**Command:** `helios_forecast/series`

**Params:** `entry_id`, `start` (ISO), `end` (ISO), `resolution_min` (defaults to
the card's "Graph detail" setting; the integration resamples server-side).

**Returns:**

```jsonc
{
  "points": [
    { "t": "2026-06-11T12:00:00+00:00",
      "pv_w": 3120,        // residual-corrected predicted power (the curve drawn)
      "pv_raw_w": 3340 }   // pre-correction (the card's forecast vs forecastRaw)
  ],
  "daily": [
    { "date": "2026-06-11", "kwh": 21.4, "kwh_raw": 22.9 }
  ]
}
```

- Future only. **Past actuals are unchanged**: the card keeps reading them from
  the recorder `change` series exactly as it does today.
- Sub-hourly, so the short shadow dips the residual map carves (a tree clipping
  production for half an hour) survive resampling. This is the fidelity the
  hourly baseline `wh_hours` cannot carry.

## 4. What the card STOPS doing in v1.9.0

- No more forecast computation: no clear-sky / transposition math, no
  client-side residual learning, no 60-day history + SoC fetch, no Open-Meteo
  fetches for the forecast (GTI, direct / diffuse, snow).
- When a forecast is configured: it reads HA's standard solar forecast for the
  baseline, and Helios-Forecast's detail series when present.
- When no forecast is configured: it draws no forecast curve and no forecast
  label, and is otherwise unchanged. The forecast is never required.
- Unchanged: weather visuals on Open-Meteo, and past actuals on the recorder
  `change` series.

## 5. Config that moves into the integration (config flow)

The card no longer carries these; they become Helios-Forecast's config entry.

- Location (defaults to the HA home).
- PV arrays: per array `tilt`, `azimuth`, `kwp`, tracker type, optional
  coordinates + height.
- Inverter max kW; inverter cutoff SoC %.
- Entity wiring for the learning loop: PV production + battery SoC. Read from the
  HA Energy dashboard preferences server-side where possible, otherwise picked in
  the flow.
- Optional solar-radiation sensor (W/m²).

---

## Settled decisions

- **Enhanced-series transport:** the WebSocket command in §3. It scales, does not
  bloat the recorder, and matches how the card already talks to HA. A state
  attribute was rejected (recorder bloat, attribute size limits at sub-hourly
  over several days).
- **Weather scope:** weather stays in the card on its own Open-Meteo path. Only
  the forecast moves out.
- **Card requirement:** none. The forecast is optional; with none configured the
  card shows no forecast curve and no forecast label, and is otherwise unchanged.
