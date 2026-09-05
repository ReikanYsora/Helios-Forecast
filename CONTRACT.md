# Data contract: Helios Solar Forecast

> Status: FROZEN (2026-06-11). This is the agreed interface between the
> integration and the Helios card. The model is ported against this schema;
> any change from here is a deliberate revision, not a moving target.
>
> Revision (2026-06-27): the config model moved from one entry holding several
> panel arrays to one entry per panel line. Every output surface is unchanged;
> only the configuration shape (section 5) and its scoping changed. Superseded
> by the 2026-09-05 revision below: an entry holds one or more lines again.
>
> Revision (2026-07-24): a panel line can carry an optional **per-line inverter cap**
> in addition to the entry-level cap (section 5). Output surfaces are unchanged.
>
> Revision (2026-08-12): automation-facing additions that do **not** change the
> card interface (sections 1 to 4 unchanged). (a) The full forecast series is
> also available to automations: a `helios_forecast.get_forecast` service and a
> `forecast` attribute on `power_now` (section 2), in addition to the card's
> WebSocket series (section 3). (b) An optional **battery state-of-charge
> projection** adds a `predicted_battery_soc` sensor and a
> `helios_forecast.get_battery_soc_forecast` service (section 2), driven by a new
> battery config block (section 5).
>
> Revision (2026-08-22): documentation correction, no interface change. Section 3
> described `helios_forecast/series` as future-only; it has always also served an
> hourly past archive, and the card has always requested it with `start` reaching
> into the past when its active period does.
>
> Revision (2026-08-22): the battery SoC projection's horizon widened from **24 h to
> 48 h** (section 2). A 24 h window cut the curve off partway through the following
> day, before its solar recovery showed. Shape unchanged (`[{datetime, soc}]`), only
> the count of points grows.
>
> Revision (2026-08-31): a panel line can carry an optional **per-line coordinate
> override** (latitude/longitude), the same optional-with-entry-level-fallback shape
> as the per-line inverter cap (section 5). Output surfaces are unchanged.
>
> Revision (2026-09-05): (a) an entry holds **one or more panel lines** (section 5):
> lines share the entry's production sensor and inverter cap, the model sums them by
> kWp share, each keeps its own orientation, optional cap and optional coordinates.
> (b) A second WebSocket command, `helios_forecast/layout` (section 3b), hands the
> card the lines' geometry. (c) The battery projection's low and high points are
> entities of their own (section 2). (d) The learning leaves curtailed hours out
> (section 5), detected from the SoC sensor and the inverter cap or from an optional
> curtailment signal entity. (e) The series split between archive and live points
> is documented as implemented (section 3).

The integration owns one **config entry per installation**, holding one or more
**panel lines** (a group of co-oriented panels each). Every surface below is scoped
to that entry: one device, one entity set and one detail series per entry, summing
its lines.

---

## How the card consumes the forecast: optional, two layers

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

## 1. Energy dashboard provider (native HA): what Helios-Forecast exposes

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

## 2. Sensors: first-class output for automations

A proper, recorder-friendly entity set, grouped under one device per config
entry. This is a primary deliverable, not a side effect: the integration is
useful **on its own** (automations, history graphs, the Energy dashboard) with
or without the Helios card, and exposing entities for automations is a very
frequently requested feature. The set mirrors what Forecast.Solar / Solcast
users expect, so people switch over without relearning, and **every value is
residual-corrected**, so it tracks the site's real behaviour better than a raw
model. The card does not depend on these entity names for its baseline layer.

Only the everyday values are **enabled by default** (`power_now`, `energy_today_remaining`,
`energy_day_1` = today, `energy_day_2` = tomorrow, `reliability`, the archive pair
`predicted_power` / `predicted_energy`, whose long-term statistics back the card's
past-forecast curve, the seven weather archive sensors and, when the battery block is
configured, `predicted_battery_soc`). The rest of the set below is registered but
**disabled by default**, so the recorder stays lean and each user enables the entities
they actually automate on; enabling one later never loses its history.

Days are numbered uniformly, **`day_1` = today** through **`day_7` = J+6**.

Power, now / next hour:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_power_now` | predicted PV power now, **W** | `device_class: power`, `state_class: measurement` |
| `sensor.helios_forecast_power_now_low` | analog P10 (low-bound) power now, **W** | disabled by default; **0** with the sun below the horizon (known, not uncertain), `null` in daylight until the analog support is solid enough to publish a band |
| `sensor.helios_forecast_power_now_high` | analog P90 (high-bound) power now, **W** | disabled by default; **0** with the sun below the horizon (known, not uncertain), `null` in daylight until the analog support is solid enough to publish a band |
| `sensor.helios_forecast_power_next_hour` | predicted average power over the next hour, **W** | |

Peak, per day over the 7-day horizon:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_peak_power_day_1` to `_day_7` | predicted peak power for day N, **W** | one entity per day, day 1 = today |
| `sensor.helios_forecast_peak_time_day_1` to `_day_7` | clock time of day N's peak | `device_class: timestamp`, one per day |

Energy, daily totals over the 7-day horizon:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_energy_day_1` to `_day_7` | predicted daily total, **kWh** | `device_class: energy` (no `state_class`: a forecast, not a metered total), one per day |
| `sensor.helios_forecast_energy_today_remaining` | predicted production left today, **kWh** | the one exception to day numbering, "remaining" only applies to today; drives "run the dishwasher if enough sun left" automations; **0** once the day's forecast is exhausted (from ~23:45 local at the default step), not `unknown` |

Energy, intraday:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_energy_this_hour` | predicted production this hour, **kWh** | |
| `sensor.helios_forecast_energy_next_hour` | predicted production next hour, **kWh** | |

Archive (enabled by default: their long-term statistics are what backs the card's
past-forecast curve, kept by HA well beyond Open-Meteo's rolling window):

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_predicted_power` | predicted PV power, **W** | `device_class: power`, `state_class: measurement`. Mirrors `power_now`; its purpose is the point the statistics import backfills from. |
| `sensor.helios_forecast_predicted_energy` | predicted energy this hour, **kWh** | `state_class: measurement` (no `device_class`: kWh + `measurement` is only valid without the energy class, the entity-bound long-term statistics need a state class). |

Forecast quality:

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_reliability` | forecast reliability, **0..100 %** | `state_class: measurement`. Blends learning maturity, recent predicted-vs-actual skill and today's cloud predictability. Attributes: `data_maturity`, `recent_skill`, `today_predictability`, `days_learned`, and a per-horizon-day `per_day` list (chart-style, kept off the recorder). |
| `sensor.helios_forecast_today_trend` | how much today's predicted total has moved since its frozen daily reference, signed **kWh** | `state_class: measurement`, disabled by default. Positive when the day now looks better than at the reference, negative when worse. Attributes: `reference_kwh`, `reference_time`, `current_kwh`, `direction`. The reference hour is a config knob (section 5). |

Battery state of charge (2026.9.0, only when the battery block in section 5 is configured):

| Entity | State | Notes |
|---|---|---|
| `sensor.helios_forecast_predicted_battery_soc` | near-term projected SoC, **0..100 %** | `device_class: battery`, `state_class: measurement`. The whole 48 h curve rides as a `forecast` attribute (`[{datetime, soc}]`, kept off the recorder), with the low and high over that window (`min_soc` / `max_soc` + times) and the forecast `reliability`. Created only when the battery feature is on. |
| `sensor.helios_forecast_battery_min_soc` / `_battery_max_soc` | the projection's lowest / highest SoC over the 48 h window, **%** | `device_class: battery`, `state_class: measurement`, disabled by default, created with the SoC sensor; `unknown` while there is no projection |
| `sensor.helios_forecast_battery_min_soc_time` / `_battery_max_soc_time` | when that low / high is reached | `device_class: timestamp`, disabled by default, same lifecycle |

Weather archive (one sensor per Open-Meteo variable the model reads, enabled by default; their
long-term statistics back the card's past weather):

| Entity | State |
|---|---|
| `sensor.helios_forecast_cloud_cover` | effective cloud cover, **%** |
| `sensor.helios_forecast_global_irradiance` / `_direct_irradiance` / `_diffuse_irradiance` | horizontal irradiance, **W/m2** |
| `sensor.helios_forecast_temperature` | **°C** |
| `sensor.helios_forecast_wind_speed` | **km/h** |
| `sensor.helios_forecast_snow_depth` | **m** |

The forecast curve and the SoC curve are also exposed as **response services** for
automations (the recommended path over scraping an attribute):

| Service | Returns |
|---|---|
| `helios_forecast.get_forecast` | `{ forecast: [{datetime, watts, p10, p90}] }`: the production curve, today onward |
| `helios_forecast.get_battery_soc_forecast` | `{ forecast: [{datetime, soc}] }`: the projected SoC, next 48 h |

Both take an optional `config_entry_id` (needed only with several installations).
The SoC projection **predicts, it never commands**: it exposes the curve and leaves
the charge decision to the user's own automation.

All PV values are residual-corrected. A raw (pre-correction) variant is not exposed
as entities to keep the set clean; the raw curve stays available to the card via
the detail series in section 3.

**Horizon.** The integration computes a 7-day forecast and every per-day entity
(energy AND peak) covers `day_1` (today) through `day_7` (J+6). Days beyond ~J+2
are inherently low-confidence for solar (cloud predictability collapses), to be
stated plainly in the docs. The Helios card's visible window is unchanged: **J-2
to J+2**, exactly as today; the extra forecast days live only in the entities.

## 3. Enhanced detail series (Helios-Forecast only): WebSocket API

The premium layer. A dense, raw + corrected curve, more than belongs in a state
attribute. Travels over a WebSocket command, the same mechanism the card already
uses to pull recorder statistics. The card uses it only when present, otherwise
it stays on the baseline `wh_hours` curve.

**Command:** `helios_forecast/series`

**Params:** `entry_id` (required), `start` and `end` (optional ISO instants with a UTC
offset; without one the call fails with `invalid_format`), `resolution_min` (optional;
the integration resamples server-side only when it is coarser than the native step,
and a resampled bucket carries `ghi` and `cloud` as `null`). An entry with no forecast
yet answers `not_found`.

**Returns:**

```jsonc
{
  "points": [
    { "t": "2026-06-11T12:00:00+00:00",
      "pv_w": 3120,        // residual-corrected predicted power (the curve drawn)
      "pv_raw_w": 3340,    // pre-correction (the card's forecast vs forecastRaw)
      "pv_p10": 2650,      // analog P10/P90 uncertainty band (null until learning is solid)
      "pv_p90": 3450,
      "ghi": 610,          // global horizontal irradiance at this bucket, W/m2
      "cloud": 32 }        // cloud cover at this bucket, %
  ],
  "daily": [
    { "date": "2026-06-11", "kwh": 21.4, "kwh_raw": 22.9 }
  ]
}
```

- Not future only: `points` also carries an **hourly past archive** (back to
  `now - LEARN_DAYS`), the same model re-run against the real historical weather,
  residual-corrected against recorded production, and analog-enriched exactly
  like the live forecast's future points (the ceiling that reins the physical
  model back down to what the site has actually produced under similar
  conditions applies here too, not just going forward). `start`/`end` bound the
  full range, past and future, in one call. **Past actuals are unchanged
  regardless**: the card's own production curve keeps reading them straight
  from the recorder `change` series, this archive only backs the *predicted*
  curve.
- Three stretches, in order: the hourly archive through the end of its last hour,
  then a clamped copy of today's elapsed stretch (the live points before now, given
  the archive's analog clamp), then the live points from now on. The live series'
  own elapsed points are never served: they are deliberately left unclamped (a past
  point there means "what the forecast said at the time") and would read as a
  nameplate plateau next to the archive.
- Hourly for the archive, sub-hourly from the end of the archive's last hour onward,
  so the short shadow dips the residual map carves (a tree clipping production for
  half an hour) survive resampling on what matters most. This is the fidelity the
  hourly baseline `wh_hours` cannot carry either way.
- The archive rebuilds once an hour (see `coordinator._last_archive_hour`), on a
  dedicated listener that fires right after the hour rolls over rather than on the
  next 30-minute refresh.

## 3b. Array layout (Helios-Forecast only): WebSocket API

The lines of an entry as geometry, for the card's array markers (one tile per line
in the scene, turned to its azimuth and tilt, at its own position).

**Command:** `helios_forecast/layout`

**Params:** `entry_id` (required). An unknown entry answers `not_found`.

**Returns:**

```jsonc
{
  "lines": [
    { "index": 0,
      "azimuth": 180,      // degrees clockwise from north
      "tilt": 30,          // degrees from horizontal
      "tracker": null,     // null = fixed, else the tracker kind string
      "share": 0.6,        // share of the entry's kWp (null when no kWp is usable)
      "kwp": 4.5,          // null when the entry has no usable kWp
      "lat": 48.0005,      // the line's own coordinates, null when it has none
      "lon": 2.0007 }      // (the consumer then falls back to `home`)
  ],
  "home": { "lat": 48.0, "lon": 2.0 }   // the entry's resolved location
}
```

## 4. What the card leaves to the integration

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

The card no longer carries these; they are Helios-Forecast's config entry. An
entry describes one installation: one or more panel lines, each with its own
orientation.

- Panel line geometry: `tilt`, `azimuth`, `kwp`, tracker type, an optional per-line
  inverter cap (kW) that clips that line before the lines are summed, and an optional
  per-line coordinate override (latitude/longitude) for a line mounted somewhere other
  than the entry's own location.
- Location (defaults to the HA home), optional per entry.
- Inverter max kW (optional clip) at the entry level, bounding the combined output of all
  lines.
- The PV production sensor that drives the learned correction. It must be a
  cumulative energy sensor (kWh) carrying long-term sum statistics: the learning
  reads hourly `change` rows from the recorder, which a power (W) sensor does not
  have. Curtailed hours are excluded from the learning as right-censored (their
  kWh is a lower bound, not what the sky allowed): from the sky-residual map when
  they fall short of the model, from the analog library altogether. An hour counts
  as curtailed when the battery's hourly maximum state of charge is at 98 % or more
  while the measured average power sits at 92 % or more of the entry-level inverter
  cap (both from the existing config), or when the curtailment signal below is on
  for at least half the hour.
- **Curtailment signal (optional):** a binary sensor, input boolean or switch that
  is on while the inverter is held back for a reason the sky cannot explain (zero
  export, a grid limit). Its hours are excluded from the learning as above.
- Today-trend reference hour: the local hour at which today's outlook reference
  is frozen.
- **Battery SoC projection (2026.9.0, optional).** A separate block, off unless
  both the usable **capacity (kWh)** and a live **state-of-charge sensor (%)** are
  set; the reserve (min SoC %), round-trip efficiency (%) and charge / discharge
  power caps (kW) are optional. The SoC sensor is the projection's starting point
  and feeds the curtailment detection above; it is never a cutoff. Home consumption
  for the projection is **not** configured here: it is derived from the Home
  Assistant Energy dashboard (solar + grid import - export + battery discharge -
  charge), averaged into a per-weekday-hour profile from recorder history.

---

## Settled decisions

- **Enhanced-series transport:** the WebSocket command in section 3. It scales, does not
  bloat the recorder, and matches how the card already talks to HA. A state
  attribute was rejected (recorder bloat, attribute size limits at sub-hourly
  over several days).
- **Weather scope:** weather stays in the card on its own Open-Meteo path. Only
  the forecast moves out.
- **Card requirement:** none. The forecast is optional; with none configured the
  card shows no forecast curve and no forecast label, and is otherwise unchanged.
