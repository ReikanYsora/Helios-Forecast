# Changelog

All notable changes to Helios Forecast are documented here. The project follows a
date-based versioning scheme (`YEAR.MONTH.PATCH`).

---

## 2026.8.0

### Added

- **Several panel lines in one entry.** A single entry can now hold more than one panel
  line, added through an "add another line" step. This is the setup for two strings on one
  inverter that only exposes a single, combined production sensor: the lines share that
  sensor and one inverter limit, and the model sums them by kWp share. One entry per line
  is still fully supported for installs that do have a sensor per line. The Configure
  button opens a menu to edit the shared settings or the lines (edit, add or remove).
  (#18)
- **`forecast` attribute on the weather sensors.** Every archived Open-Meteo sensor —
  cloud cover, irradiance, temperature, wind, snow — now carries a forward-looking hourly
  `forecast` list (today plus the 7-day horizon), the same shape the power sensor exposes,
  so you can plot the upcoming sky next to the predicted power in ApexCharts. (#21)

### Fixed

- **Decimal peak power and inverter limits are entered reliably.** The peak power, tilt,
  azimuth and inverter fields are now proper numeric inputs with a defined decimal step,
  so a value like `2.61 kWp` is stored at full precision instead of being misread by the
  browser's number locale. (#13)
- **A transient Open-Meteo blank no longer errors.** An empty or rate-limited weather
  response is now retried a few times, and if it still comes back empty the last good
  fetch is reused for that cycle rather than failing the update with "Open-Meteo returned
  no weather data". (#19)
- **The production sensor picker only offers energy sensors now.** The learned correction
  reads the sensor's long-term sum statistics (hourly kWh change); a power sensor (W) has
  none, which silently disabled the learning and capped the reliability index at ~36 %.
  The picker is filtered to energy sensors, the field explains what to pick (a cumulative
  kWh sensor, e.g. the inverter's total production), and installs that already point at a
  sensor without usable statistics now log an explicit warning instead of failing
  silently.

---

## 2026.7.2

### Fixed

- **Setup no longer crashes on a missing cloud hour.** Open-Meteo can leave an hour with
  no cloud value (`None`); the sky-residual code then raised `TypeError`, failing the
  coordinator update in a loop (`setup_retry`) whenever a production sensor was
  configured. A missing hour is now treated as clear sky. (#14)

### Improved

- **Reliability degrades gently across the horizon.** The per-day reliability used a steep
  12 %/day linear decay that bottomed out at its floor by day +5. It now decays
  exponentially toward a higher floor (day 0 = 100 %, day +3 ~71 %, day +6 ~58 %), which
  matches how weather-model skill actually degrades, so the later days are no longer
  under-rated. (#16)
- **The learned correction now accounts for outdoor temperature.** Panels lose output as
  they heat up (about 0.35 %/degC of cell temperature). The analog ensemble now matches
  past hours on temperature as well as cloud cover and sun geometry, so a hot day and a
  cool day with the same sky are no longer averaged together. (#17)

### Internal

- Full strict cleanup pass: ruff lint and formatting, a fully type-checked package (mypy),
  and a CI lint job so it stays clean. No behaviour change.

---

## 2026.7.1

First HACS release of Helios Forecast: a native solar-production forecast provider for the
Home Assistant Energy dashboard, a clean set of sensors (power now and next hour, per-day
energy and peaks over a 7-day horizon, energy left today), and a sub-hourly detail series
the Helios card reads. Configuration is one entry per panel line, so each roof orientation
gets its own forecast, device and entities.
