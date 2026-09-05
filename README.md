<div align="center">

<img src="https://raw.githubusercontent.com/ReikanYsora/Helios-Forecast/main/images/helios-forecast-logo.svg" alt="" width="84">

# HELIOS FORECAST

**A solar forecast that learns from your panels.**

A Home Assistant integration that predicts your PV production, corrects itself
against what your installation really produces, and publishes the result where
Home Assistant already knows how to show it.

<img src="https://raw.githubusercontent.com/ReikanYsora/Helios-Forecast/main/images/forecast-vs-reality.png" alt="The Helios card at midday: the measured production curve and the forecast running together, the readout showing 2055 W produced against 2062 W predicted." width="880">

<sub>Measured **2 055 W**, predicted **2 062 W**. Seven watts apart, on a real installation.</sub>

[**Learn more**](https://helios-ha.org/helios-forecast/) &nbsp;&nbsp; [**Data contract**](CONTRACT.md) &nbsp;&nbsp; [**Changelog**](CHANGELOG.md)

[![Release](https://img.shields.io/github/v/release/ReikanYsora/Helios-Forecast?display_name=tag&style=for-the-badge&color=e0a106)](https://github.com/ReikanYsora/Helios-Forecast/releases)
[![HACS Default](https://img.shields.io/badge/HACS-Default-e0a106.svg?style=for-the-badge)](https://github.com/hacs/default)
[![Validate](https://img.shields.io/github/actions/workflow/status/ReikanYsora/Helios-Forecast/validate.yml?style=for-the-badge&label=validate&color=e0a106)](https://github.com/ReikanYsora/Helios-Forecast/actions/workflows/validate.yml)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.1%2B-e0a106.svg?style=for-the-badge&logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![License](https://img.shields.io/github/license/ReikanYsora/Helios-Forecast?style=for-the-badge&color=blue)](https://www.gnu.org/licenses/gpl-3.0)
[![Stars](https://img.shields.io/github/stars/ReikanYsora/Helios-Forecast?style=for-the-badge&color=e0a106)](https://github.com/ReikanYsora/Helios-Forecast/stargazers)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=000000)](https://www.buymeacoffee.com/reikanysora)

</div>

---

## Install

Helios Forecast is in the **HACS default store**.

1. In **HACS**, search for **Helios Forecast** and install it.
2. **Restart Home Assistant.**
3. Go to **Settings** > **Devices and services** > **Add integration**, and search for **Helios Forecast**.
4. Fill in your first panel line: orientation, peak power, and your PV production sensor.

> The production sensor must be a **cumulative energy sensor in kWh**, your
> inverter's or meter's running total, not an instantaneous power sensor in W.
> Only an energy sensor carries the long-term statistics the learning reads.

<details>
<summary>About the "custom integration" warning in your log</summary>

<br>

Home Assistant logs `We found a custom integration helios_forecast which has not
been tested by Home Assistant` at startup. That notice appears for every
integration installed outside of core, which means every HACS integration. It is
informational and does not indicate a problem.

</details>

---

## What you get

**A native provider for the Energy dashboard.** The prediction appears in Home
Assistant's own Energy view, right next to your real production. No card
required, no extra setup.

**Real entities, built for the recorder.** Power now, next hour, per-day energy
and peaks over a seven day horizon, energy left today, and a forecast
reliability score. All usable straight away in automations, history graphs and
template sensors.

**A sub-hourly detail series** the [Helios card](https://github.com/ReikanYsora/Helios)
reads for full-fidelity visuals, so the card does not have to compute a forecast
of its own.

Every entity is listed on the [website](https://helios-ha.org/helios-forecast/).

---

## The full forecast, in your automations

Helios Forecast computes the **whole production curve**, not just the current
value: one point every 15 minutes, from midnight today through the seven-day
horizon, each with the predicted watts and its **P10/P90 confidence band**. An
Energy Management System can read it to shift battery charging into the expected
peak, work out how much sun is still to come, or find the latest safe charging
start.

**As a service (recommended).** `helios_forecast.get_forecast` returns the curve
as response data, on demand:

```yaml
# How much PV energy is still forecast for the rest of today
script:
  sun_left_today:
    sequence:
      - action: helios_forecast.get_forecast
        response_variable: helios
      - variables:
          remaining_kwh: >
            {{ (helios.forecast
                 | selectattr('datetime', 'match', now().strftime('%Y-%m-%d'))
                 | selectattr('datetime', 'ge', now().isoformat())
                 | map(attribute='watts') | map('float') | sum
                 * 0.25 / 1000) | round(1) }}
      - action: persistent_notification.create
        data:
          title: Solar forecast
          message: "About {{ remaining_kwh }} kWh of sun still to come today."
```

The response is a list of buckets (`p10` / `p90` are `null` until the forecast has
learned enough of your history to publish a band):

```yaml
forecast:
  - datetime: "2026-09-12T14:00:00+02:00"
    watts: 3120.4
    p10: 2610.0
    p90: 3450.8
  - datetime: "2026-09-12T14:15:00+02:00"
    watts: 3038.1
    p10: 2550.2
    p90: 3380.5
  # ... one point every 15 minutes
```

With more than one installation, pass `config_entry_id` (from **Settings** >
**Devices and services**) to choose which one to read.

**As an attribute.** The same curve rides on the `power_now` sensor as its
`forecast` attribute, handy in a template sensor:

```jinja
{{ state_attr('sensor.helios_forecast_power_now', 'forecast') }}
```

---

## Predicted battery state of charge

If you have a battery, Helios Forecast can project its **state of charge over the
next 48 hours**. It runs the production forecast against your home's own
consumption, which it derives from your **Home Assistant Energy dashboard** (no
extra sensor to wire), and integrates the battery's charge from your current SoC.

Turn it on in the integration settings by filling in your **battery capacity** and
your **state-of-charge sensor**; the reserve, efficiency and charge/discharge
limits are optional. A **Predicted battery state of charge** sensor then appears,
carrying the full curve as its `forecast` attribute (plus the projected low and
high over those 48 hours, when each is reached, and the forecast reliability so you
know how far to trust it). Four companion entities, disabled by default, carry the
same low and high with their times (`battery_min_soc`, `battery_min_soc_time`,
`battery_max_soc`, `battery_max_soc_time`) for a tile or an automation.

It predicts, it never commands. Sending the charge order stays with your own
automation, which knows your inverter; Helios just tells it what's coming:

```yaml
# Top the battery up from the grid tonight only if it's predicted to run low
- action: helios_forecast.get_battery_soc_forecast
  response_variable: soc
- variables:
    lowest: "{{ soc.forecast | map(attribute='soc') | map('float') | min }}"
- condition: template
  value_template: "{{ lowest < 20 }}"
- action: switch.turn_on
  target:
    entity_id: switch.force_battery_charge
```

The response:

```yaml
forecast:
  - datetime: "2026-09-12T14:00:00+02:00"
    soc: 78.4
  - datetime: "2026-09-12T14:15:00+02:00"
    soc: 80.1
  # ... one point every 15 minutes, 48 hours ahead
```

One honest note: home consumption is a **learned average**, so the projection is a
good steer, not a guarantee: read it alongside the reliability figure, and it gets
better as it sees more of your history.

---

## How it learns

It starts from physics: Open-Meteo horizontal irradiance transposed onto each panel
orientation, the direct and diffuse split, snow cover, combined with your
installation geometry and a cell-temperature derate so hot days are not
over-predicted.

Then it stops trusting the physics alone. It learns a correction from your home's
own recorded production, matching past hours on cloud cover, sun geometry and
outdoor temperature. Over time the prediction absorbs what no generic model can
know about your site: shading, soiling, an orientation that is a few degrees off.

What it does not learn is your hardware's limits. An hour where the inverter was
held back, by a full battery, a zero-export rule or a grid limit, says nothing
about the sky, so it is left out of the learning (out of the sky correction when it
falls short of the model, out of the analog library altogether) and the limits stay
where the forecast already applies them: forward, at forecast time. A full battery
is recognised on its own from the state of charge sensor and the entry-level
inverter cap you configured; for what the integration cannot see (zero export, a
grid limit), point
the optional **curtailment signal** at a binary sensor, input boolean or switch
that is on while the inverter is being held back.

> On a DC-coupled hybrid, learn from a production reading taken **before** the
> battery tap (inverter DC power plus battery DC power, integrated to kWh), not
> from the AC meter: the AC side never sees the energy that went straight into
> the battery, and learns nightly discharge as production.

It also publishes a **reliability score**, which reflects how much history backs
the learning, how accurate it has been recently, and how predictable today's sky
is. A forecast that tells you when to doubt it is worth more than one that does
not.

Everything runs on your server. No account, no API key, no browser-side maths.

---

## Panel lines

A panel line is a group of co-oriented panels. There are two ways to describe
your installation, and the right one depends on your sensors.

**One entry per line**, recommended when each line has its own production sensor.
Each entry gets its own device, entities and detail series, so every line can be
wired to its own card. Add the integration once per line.

**Several lines in one entry**, for two strings on one inverter that reports a
single combined value. Add the first line, then tick *add another line* for each
extra orientation. The lines share one production sensor and one inverter limit,
and the forecast sums them by their kWp share. Use **Configure** to edit the
shared settings or the lines later. Each line can also override the shared
inverter cap and GPS coordinates, for a micro-inverter string that saturates on
its own or a line mounted somewhere other than the entry's home location.

---

## Works with the Helios card

**[Helios](https://github.com/ReikanYsora/Helios)** turns your Energy dashboard
into a live 2.5D scene: the sun crossing its arc over your home, your production,
grid and battery flowing in real time. It draws this integration's prediction as
the dashed curve your measured production tracks against, so you see the forecast
and the reality on the same picture. From card 2026.9.4 it also reads each panel
line's orientation and position from this integration to mark your arrays in the
scene.

Neither needs the other. Together they are the whole story.

---

## Status and documentation

Early days, and moving fast. The data contract the card is built against is
documented and frozen, so a change on either side cannot silently break the other.

| | |
| :-- | :-- |
| [CONTRACT.md](CONTRACT.md) | The data contract between the integration and the card |
| [CHANGELOG.md](CHANGELOG.md) | What changed, release by release |
| [helios-ha.org](https://helios-ha.org/helios-forecast/) | The full entity list and how it all fits together |

Requires Home Assistant **2025.1.0** or later.

Found a bug, or is a forecast off? [Open an issue](https://github.com/ReikanYsora/Helios-Forecast/issues).
Feedback is very welcome, and it is what shapes the roadmap.

---

## Support the project

Helios Forecast is built and maintained by one person, in the open, and given
away. A **star** costs nothing and helps other people find it. A coffee keeps the
next cycle going.

<div align="center">
<a href="https://www.buymeacoffee.com/reikanysora"><img src="https://img.buymeacoffee.com/button-api/?text=Support this project&emoji=☀️&slug=reikanysora&button_colour=5F7FFF&font_colour=ffffff&font_family=Arial&outline_colour=000000&coffee_colour=FFDD00" alt="Buy me a coffee"></a>
</div>

---

## Special thanks

- [antoineguilbert.fr](https://www.antoineguilbert.fr/helios-home-assistant-carte-3d-avec-lidar/) ([Helios Forecast](https://www.antoineguilbert.fr/prevision-solaire-home-assistant-avec-helios-forecast/))
- [Glooob Domo](https://www.youtube.com/watch?v=bTg4mzb9jwA)
- [Smart-Live](https://youtu.be/zFbppiAmCr0)

---

## License

Helios Forecast, a self-learning solar forecast integration for Home Assistant.
Copyright (C) 2026 Jérôme CREMOUX (ReikanYsora).

Licensed under the GNU General Public License v3.0, see [LICENSE](LICENSE).
