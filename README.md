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

## How it learns

It starts from physics: Open-Meteo irradiance, global tilted irradiance per panel
orientation, the direct and diffuse split, snow cover, combined with your
installation geometry and a cell-temperature derate so hot days are not
over-predicted.

Then it stops trusting the physics alone. It learns a correction from your home's
own recorded production, matching past hours on cloud cover, sun geometry and
outdoor temperature. Over time the prediction absorbs what no generic model can
know about your site: shading, soiling, an orientation that is a few degrees off,
inverter clipping, even battery curtailment.

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
shared settings or the lines later.

---

## Works with the Helios card

**[Helios](https://github.com/ReikanYsora/Helios)** turns your Energy dashboard
into a live 2.5D scene: the sun crossing its arc over your home, your production,
grid and battery flowing in real time. It draws this integration's prediction as
the dashed curve your measured production tracks against, so you see the forecast
and the reality on the same picture.

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

## License

Helios Forecast, a self-learning solar forecast integration for Home Assistant.
Copyright (C) 2026 Jérôme CREMOUX (ReikanYsora).

Licensed under the GNU General Public License v3.0, see [LICENSE](LICENSE).
