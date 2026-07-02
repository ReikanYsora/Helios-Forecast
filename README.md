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

## One forecast per panel line

Each Helios Forecast entry describes a single panel line (a group of
co-oriented panels) and gets its own device, its own entities, and its own
detail series. If your roof faces several directions, add the integration once
per line. That way every line can be wired to its own card on the dashboard.

## Installation

This integration is installed through [HACS](https://hacs.xyz/).

1. In HACS, add this repository as a custom integration (category: Integration),
   or install it from the default list once it is published there.
2. Restart Home Assistant.
3. Go to Settings, Devices and Services, Add Integration, and search for
   "Helios Forecast".
4. Fill in the panel line: orientation, peak power, optional location and
   inverter limit, and the PV production sensor that drives the learned
   correction.

## Status

Early days, and moving fast. The data contract the card is built against is
documented and frozen in [CONTRACT.md](./CONTRACT.md), and per-release changes are
in [CHANGELOG.md](./CHANGELOG.md). Feedback and issues are very welcome.
