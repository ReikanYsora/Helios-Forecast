# Helios Forecast

☀️ An accurate, self-learning solar production forecast for Home Assistant.

It computes a PV production forecast server-side and publishes it to two places:

1. **The official Energy dashboard** , it registers as a native solar
   production forecast provider, so the prediction shows up in Home Assistant's
   own Energy view alongside your real production.
2. **The [Helios card](https://github.com/ReikanYsora/Helios)** , it exposes a
   detailed forecast series the card reads, so the card no longer computes the
   forecast in the browser.

The forecast combines Open-Meteo irradiance (global tilted irradiance per panel
orientation, direct / diffuse split, snow cover) with the installation geometry,
then learns a correction from the home's own recorded production so the
prediction tracks the site's real behaviour over time.

> Status: early development. The data contract the card builds against is being
> frozen in [CONTRACT.md](./CONTRACT.md).
