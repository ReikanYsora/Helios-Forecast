# Replay

Re-forecast past days offline, as the integration would have forecast them that morning, and
score them against the meter. The residual map and the analog library are rebuilt from the
history available at 06:00 local; the archived weather stands in for the forecast weather, so the
weather error is out of the picture on purpose. What this measures is the learning.

    HA_HOST=192.168.0.2:8123 HA_TOKEN=... PRODUCTION=sensor.your_meter_energy node tools/replay/fetch.mjs
    .venv/bin/python tools/replay/replay.py --days 30

`replay.py` carries the installation's layout as constants; edit them for another roof. `data/`
stays out of git. Variants of the analog step live in `replay.py` so a change to the learning is
measured on sixty days before it is written into the integration.

`pipeline actuel` is the integration's own path (ratio analogs since 2026.9.3); `pipeline 2026.9.2 (watts)` keeps the previous watts library for comparison.
