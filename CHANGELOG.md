# Changelog

All notable changes to Helios Forecast are documented here. The project follows a
date-based versioning scheme (`YEAR.MONTH.PATCH`).

---

## 2026.9.0

A release about putting the forecast to work: the full curve now reaches your
automations, a battery can be projected forward, and the forecast now reads the
sky exactly like the Helios card.

### Added: the full forecast, in your automations

The integration already computes the whole production curve, not just the current
value. It's now available to automations two ways: a **`helios_forecast.get_forecast`
service** that returns the remaining curve on demand (15-minute points, each with
the predicted watts and the P10/P90 confidence band), and the same curve as a
`forecast` attribute on the `power_now` sensor. An energy management system can
read it to shift battery charging into the expected peak, work out how much sun is
still to come, or find the latest safe charging start. Thanks to the detailed
write-up on #35.

### Added: a predicted battery state of charge

If you have a battery, Helios Forecast can now project its **state of charge over
the next 24 hours**. It runs the production forecast against your home's own
consumption — derived straight from your **Home Assistant Energy dashboard**, so
there's no extra sensor to wire — and integrates the battery's charge from your
current level, within your capacity, reserve and charge/discharge limits. Turn it
on by filling in your battery capacity and state-of-charge sensor in the
integration settings; a **Predicted battery state of charge** sensor then appears,
carrying the full curve, the day's projected low and high, and the forecast
reliability. There's also a `helios_forecast.get_battery_soc_forecast` service.

It predicts, it never commands: sending the charge order stays with your own
automation, which knows your inverter. And because home consumption is a learned
average, it's an honest steer read alongside the reliability figure, not a
guarantee. Thanks to @brunnwart and @jasonyates for the design brief (#25).

### Changed: the forecast transposes the sun itself, no GTI dependency

Helios Forecast no longer pulls a global tilted-irradiance (plane-of-array)
supply; it transposes the horizontal irradiance onto each panel plane itself,
exactly like the Helios card does. One fewer external dependency, and the two
stay in lockstep. It also removes a flip-flop where some refreshes used
Open-Meteo's own tilted value and others a fallback, giving different
magnitudes from one update to the next.

### Changed: the weather request now matches the Helios card

The Open-Meteo request mirrors the card's: the same model picker (a regional
high-resolution model paired with a global one, median-fused), the same weighted
cloud-cover layers, and instant irradiance. Card and forecast read the same sky.

### Fixed: ready for a future Home Assistant statistics change

A coming Home Assistant version tightens what the long-term statistics import
expects; the integration now declares the new fields up front, so the archived
weather and predicted-production history keep importing cleanly across the change.

### Fixed: the battery SoC projection could crash, and now says why it skips

The state-of-charge projection could error out, and when it correctly declined
to run because an input was missing it did so silently, so an empty
`get_battery_soc_forecast` gave no clue why. It no longer crashes, and it logs a
clear reason whenever the projection is off, so you can tell at a glance what to
fill in. Thanks to @FoxP (#40).

### Fixed: the SoC projection recovers the moment the battery sensor is back

On startup a battery integration can leave its state-of-charge sensor unavailable
for a few seconds while it connects (a modbus link, for instance). The projection
read the sensor at that instant, found it unavailable and stayed off until the
next 30-minute refresh. It now re-projects as soon as the sensor becomes available
again, and a brief startup gap is logged gently rather than as a warning. Thanks
to @FoxP (#42).
No user action needed. Thanks to @FoxP for pointing out the breakage radar (#38).

### Fixed: border locations pick the right regional weather model

The forecast pairs a global weather model with the best regional high-resolution
one for your location, using the same picker as the Helios card. The regional
coverage areas overlap at national borders, and the first area listed used to
win, so a site near a border, or inside a smaller area enclosed by a larger one,
could be read with a neighbour's model. It now picks the area your location sits
most centrally within, so the regional model matches where you actually are.
Thanks to @MatCos.

### Fixed: wind speed was read in the wrong unit, running the forecast hot

Open-Meteo returns wind at 10 m in km/h, but the cell-temperature model that
derates the forecast for a hot panel expects m/s. Reading the km/h figure as
if it were m/s made the modelled cell run too cool, which overstated the
forecast: about 7% high at a moderate 5 m/s wind, and 14% at 10 m/s. Converted
at the single point wind enters the model; your wind sensor and its history
are untouched, still in km/h. Thanks to @Happyfield7 (#44, #45).

### Changed: the battery SoC projection now looks 48 hours ahead

The projection used to stop 24 hours out, which cut it off partway through the
following day, before that day's solar had a chance to recharge the battery. A
chart reading past that point just saw the reserve floor, looking stuck there
even though the battery would genuinely recover once the sun came back. It now
runs 48 hours ahead, using PV forecast points already being fetched. Thanks to
@FoxP (#40, #41).

---

## 2026.8.3

A performance and reliability release on top of 2026.8.2.

### Fixed: high CPU and network stalls every 30 minutes

Since 2026.8.2, each 30-minute refresh ran its whole forecast computation on Home Assistant's event
loop and rebuilt the full 60-day predicted-production archive every time, which briefly pinned a CPU
core to 100% and could stall the network for a few seconds on small systems. The refresh now runs its
heavy work off the event loop, rebuilds the 60-day archive at most once an hour, memoises the
sun-position maths, caps the concurrent Open-Meteo requests, writes the weather statistics
incrementally instead of re-importing 60 days every time, and precomputes each refresh's time axes
once instead of rebuilding them on every sample. Thanks to the users who reported it with detailed
CPU traces.

### Fixed: forecast sensors could stay unavailable until a manual update

A stalled network request had no timeout, so if a fetch hung, that config entry's refresh could
freeze and leave its sensors unavailable for a long time until `homeassistant.update_entity` was
called by hand (#32). Requests now time out and fall back to the retry and last-good-value path, so a
temporary network problem recovers on its own at the next cycle. Thanks to @CaneTLOTW for the
detailed report.

---

## 2026.8.2

A corrective release on top of 2026.8.1.

### Fixed

- **Faster startup.** The integration fetched each panel orientation's irradiance from
  Open-Meteo one after another and ran the full 60-day statistics backfill during setup, so a
  multi-orientation install was slow to appear. It now fetches every orientation in parallel
  and moves the archiving to a background task, so setup finishes promptly. (#31) Thanks to
  @FoxP.

- **Over-prediction curbed on shaded sites.** The physical model cannot see near-field shadows
  (a tree in the morning, a roof in the evening), so at low learning confidence the forecast
  could predict well above what a site actually produces. Once enough close analogs exist, the
  forecast is now capped at the site's own observed production under similar sun and cloud (with
  a margin), which reins in the over-prediction while still allowing an unusually clear day.
  (#28) Thanks to @ManuMaxGit and @ferreto1978.

### Changed

- **A leaner default entity set.** A fresh install added a large entity set to the recorder.
  Only the everyday values are now enabled by default (power now, energy today remaining, today
  and tomorrow energy, and reliability); the rest are registered but disabled, so you switch on
  only what you automate on, and enabling one later never loses its history. (#30) Thanks to
  @rapahl.

### Packaging

- Installable from the **HACS default store**, with an integration icon shown on the HACS
  repository line, and a refreshed README (absolute images so they render in HACS, restyled
  badges).

---

## 2026.8.1

### Added

- **A per-line inverter limit.** Each panel line can now carry its own optional inverter
  cap, on top of the entry-level one. When set, the line is clipped at its own ceiling
  before the lines are summed, so a micro-inverter string that saturates on its own is
  modelled correctly instead of being bounded only on the combined total. Left empty, a
  line is uncapped and behaves exactly as before. (#26)

### Fixed

- **Cloud cover now matches the Open-Meteo app.** The weather inputs were blended across
  several forecast models and reduced to their median, which could outvote the one model
  that is actually right for a location and, for instance, show a fully overcast tomorrow
  as clear. The values now come from best_match, the same single model the Open-Meteo app
  displays and the one the tilted-irradiance request already used, so the cloud curve lines
  up with what you see in the app. The cross-model spread is still read as an uncertainty
  signal for the reliability index. (#22)
- **The forecast no longer reads sunnier than the Helios card.** Because of that same model
  blend, the forecast could sit a little above the card's own figure and over-estimate
  production. With the inputs back on best_match, the integration and the card read the same
  source and track much more closely. (#27)

### Changed

- Refreshed the integration branding (icon and logo).

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
