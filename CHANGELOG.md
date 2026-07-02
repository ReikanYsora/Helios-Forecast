# Changelog

All notable changes to Helios Forecast are documented here. The project follows a
date-based versioning scheme (`YEAR.MONTH.PATCH`).

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
