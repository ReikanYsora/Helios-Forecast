# Helios Forecast

☀️ An accurate, self-learning solar production forecast for Home Assistant.

Helios Forecast computes your PV production forecast on the server, learns from
what your installation actually produces, and publishes the result in the places
Home Assistant already knows how to show it. No browser-side math, no guessing.

## What it does

- **Feeds the official Energy dashboard.** It registers as a native solar
  production forecast provider, so the prediction shows up right next to your
  real production in Home Assistant's own Energy view.
- **Gives you real entities.** A clean, recorder-friendly set of sensors (power
  now, next hour, per-day energy and peaks over a 7 day horizon, energy left
  today, plus a forecast-reliability score) that you can drop straight into
  automations and history graphs. Useful on its own, with or without the card.
- **Powers the [Helios card](https://github.com/ReikanYsora/Helios).** It serves
  a richer, sub-hourly detail series the card reads for full fidelity visuals,
  so the card no longer has to compute the forecast itself.

## How it works

The forecast starts from Open-Meteo irradiance (global tilted irradiance per
panel orientation, the direct and diffuse split, snow cover) combined with your
installation geometry, including a cell-temperature derate so hot days are not
over-predicted. Then it learns a correction from your home's own recorded
production, matching past hours on cloud cover, sun geometry and outdoor
temperature, so over time the prediction tracks your site's real behaviour:
shading, soiling, orientation error, inverter clipping, even battery curtailment.
It also publishes a reliability score that reflects how much history backs the
learning, its recent accuracy, and how predictable today's sky is.

## Panel lines

A panel line is a group of co-oriented panels. You can organise your install two
ways:

- **One entry per line** (recommended when each line has its own production
  sensor). Each entry gets its own device, entities and detail series, so every
  line can be wired to its own card on the dashboard. Add the integration once
  per line.
- **Several lines in one entry** (for two strings on one inverter that only
  reports a single, combined production value). Add the first line, then tick
  "add another line" for each extra orientation. The lines share one production
  sensor and one inverter limit; the forecast sums them by their kWp share. Use
  the Configure button to edit the shared settings or the lines later.

## Installation

This integration is installed through [HACS](https://hacs.xyz/).

1. In HACS, add this repository as a custom integration (category: Integration),
   or install it from the default list once it is published there.
2. Restart Home Assistant.
3. Go to Settings, Devices and Services, Add Integration, and search for
   "Helios Forecast".
4. Fill in the panel line: orientation, peak power, optional location and
   inverter limit, and the PV production sensor that drives the learned
   correction. If several strings share one inverter and one production sensor,
   tick "add another line" and add each orientation to the same entry.

> Home Assistant logs "We found a custom integration helios_forecast which has
> not been tested by Home Assistant" on startup. That notice is shown for every
> integration installed outside of core (all HACS integrations) and is
> informational — it does not indicate a problem with Helios Forecast.

## Status

Early days, and moving fast. The data contract the card is built against is
documented and frozen in [CONTRACT.md](./CONTRACT.md), and per-release changes are
in [CHANGELOG.md](./CHANGELOG.md). Feedback and issues are very welcome.

---

## License

Helios Forecast, self-learning solar forecast integration for Home Assistant.
Copyright (C) 2026 Jérôme Crémoux (ReikanYsora).

This project is licensed under the GNU General Public License v3.0, see the [LICENSE](LICENSE) file for details.
