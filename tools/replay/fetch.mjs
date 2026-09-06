// Pull the replay's inputs from a Home Assistant instance over its websocket API, on Node's built-in
// WebSocket: the production meter's hourly energy and the seven weather fields the integration
// archives. Writes data/production.json and data/weather.json next to this file.
//
//   HA_HOST=192.168.0.2:8123 HA_TOKEN=... PRODUCTION=sensor.your_meter_energy node tools/replay/fetch.mjs
import { haCall } from "./ha-ws.mjs";
import fs from "node:fs";
import path from "node:path";
const here = path.dirname(new URL(import.meta.url).pathname);
const production = process.env.PRODUCTION;
if (!process.env.HA_TOKEN || !production) { console.error("HA_TOKEN and PRODUCTION are required"); process.exit(1); }
const days = Number(process.env.DAYS ?? 75);
const end = new Date(); const start = new Date(end.getTime() - days * 86400e3);
const common = { type: "recorder/statistics_during_period", start_time: start.toISOString(), end_time: end.toISOString(), period: "hour" };
const prod = await haCall({ ...common, statistic_ids: [production], types: ["change"], units: { energy: "kWh" } });
fs.mkdirSync(path.join(here, "data"), { recursive: true });
fs.writeFileSync(path.join(here, "data", "production.json"), JSON.stringify((prod[production] ?? []).map((r) => [r.start, r.end, r.change])));
const fields = ["cloud_cover", "global_irradiance", "direct_irradiance", "diffuse_irradiance", "temperature", "wind_speed", "snow_depth"];
const wx = await haCall({ ...common, statistic_ids: fields.map((f) => `sensor.helios_forecast_${f}`), types: ["mean"] });
const out = {};
for (const [id, rows] of Object.entries(wx)) out[id.replace("sensor.helios_forecast_", "")] = rows.map((r) => [r.start, r.mean]);
fs.writeFileSync(path.join(here, "data", "weather.json"), JSON.stringify(out));
console.log(`production: ${(prod[production] ?? []).length} hours | weather: ${Object.values(out).map((v) => v.length).join("/")} hours`);
