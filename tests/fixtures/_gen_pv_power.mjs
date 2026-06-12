// Golden-fixture generator for the PV power parity test.
//
// Mirrors the card's computePvPower (src/engine/sun.ts) plus the cell-temperature
// helpers (src/engine/pv-thermal.ts) so the Python port in
// custom_components/helios_forecast/solar/irradiance.py can be checked to produce
// the exact same percentage across every branch. Run from the repo root:
//
//   node tests/fixtures/_gen_pv_power.mjs > tests/fixtures/pv_power.json
//
// Each row carries the full input (date, location, cloud, panel, ctx) and the
// expected percentage, so the Python test rebuilds the same call and asserts.

const NOCT_CELL_C = 44, NOCT_IRRADIANCE = 800, NOCT_AIR_REF_C = 20, WIND_COOLING_K = 1.5;
const GAMMA_PMP_PER_C = -0.0035, STC_REF_C = 25;

function cellTemperatureC(airTempC, ghiWm2, windMs)
{
    if (!isFinite(airTempC)) { return NaN; }
    const g = Math.max(0, ghiWm2);
    const w = isFinite(windMs) ? Math.max(0, windMs) : 0;
    return airTempC + (NOCT_CELL_C - NOCT_AIR_REF_C) / NOCT_IRRADIANCE * g - WIND_COOLING_K * w;
}

function thermalDerating(cellTempC)
{
    if (!isFinite(cellTempC)) { return 1; }
    return Math.max(0.6, 1 + GAMMA_PMP_PER_C * (cellTempC - STC_REF_C));
}

function getSunPosition(date, lat, lon)
{
    const D    = Math.PI / 180;
    const H    = date.getUTCHours() + date.getUTCMinutes() / 60 + date.getUTCSeconds() / 3600;
    const doy  = Math.floor((date.getTime() - Date.UTC(date.getUTCFullYear(), 0, 0)) / 86_400_000);
    const decl = 23.45 * Math.sin(D * (360 / 365) * (doy - 81));
    const B    = D * (360 / 365) * (doy - 81);
    const eot  = 9.87 * Math.sin(2 * B) - 7.53 * Math.cos(B) - 1.5 * Math.sin(B);
    let ha = 15 * (H + lon / 15 + eot / 60 - 12);
    ha = ((ha + 180) % 360 + 360) % 360 - 180;
    const sinA = Math.sin(D * lat) * Math.sin(D * decl)
               + Math.cos(D * lat) * Math.cos(D * decl) * Math.cos(D * ha);
    const alt  = Math.asin(Math.max(-1, Math.min(1, sinA))) / D;
    const cAlt = Math.cos(alt * D);
    const cAz  = cAlt > 1e-4 ? (Math.sin(D * decl) - Math.sin(D * lat) * sinA) / (Math.cos(D * lat) * cAlt) : 0;
    let az = Math.acos(Math.max(-1, Math.min(1, cAz))) / D;
    if (ha > 0) { az = 360 - az; }
    return { altitude: alt, azimuth: az };
}

function computePvPower(date, lat, lon, cloudCoverPct, panel, ctx)
{
    const sun = getSunPosition(date, lat, lon);
    const alt = sun.altitude;
    if (alt <= 0) { return 0; }
    const D    = Math.PI / 180;
    const cosZ = Math.sin(alt * D);
    const ghiClear = 1098 * cosZ * Math.exp(-0.059 / cosZ);
    const cc     = Math.max(0, Math.min(100, cloudCoverPct)) / 100;
    const kCloud = 1 - 0.75 * Math.pow(cc, 3.4);
    const ghiEff = (ctx?.ghiWm2 != null && ctx.ghiWm2 >= 0) ? ctx.ghiWm2 : ghiClear * kCloud;
    let poaEff;
    if (!panel || (panel.tiltDeg <= 0 && !panel.tracker))
    {
        poaEff = ctx?.shading ? ghiEff * 0.25 : ghiEff;
    }
    else
    {
        let beta_deg = panel.tiltDeg;
        let az_deg   = panel.azimuthDeg;
        if (panel.tracker === 'dual-axis')        { beta_deg = 90 - alt; az_deg = sun.azimuth; }
        else if (panel.tracker === 'single-axis-h') { beta_deg = 90 - alt; }
        else if (panel.tracker === 'single-axis-v') { az_deg = sun.azimuth; }
        const beta = beta_deg * D;
        const dAz  = (sun.azimuth - az_deg) * D;
        const altR = alt * D;
        const cosTheta = Math.sin(altR) * Math.cos(beta) + Math.cos(altR) * Math.sin(beta) * Math.cos(dAz);
        const Rb = cosTheta > 0 ? Math.max(0, cosTheta) / Math.max(0.087, cosZ) : 0;
        const hasSplit = ctx?.directWm2 != null && ctx.directWm2 >= 0
                      && ctx?.diffuseWm2 != null && ctx.diffuseWm2 >= 0
                      && (ctx.directWm2 + ctx.diffuseWm2) > 0;
        let directFraction;
        if (hasSplit) { directFraction = ctx.directWm2 / (ctx.directWm2 + ctx.diffuseWm2); }
        else { directFraction = Math.max(0, Math.min(0.85, (kCloud - 0.25) / 0.75 * 0.85)); }
        const diffuseFraction = 1 - directFraction;
        const directPoa  = ctx?.shading ? 0 : ghiEff * directFraction * Rb;
        const diffusePoa = ghiEff * diffuseFraction * (1 + Math.cos(beta)) / 2;
        const groundPoa  = ghiEff * 0.2 * (1 - Math.cos(beta)) / 2;
        if (ctx?.poaWm2 != null && ctx.poaWm2 >= 0)
        {
            poaEff = ctx.shading ? Math.min(ctx.poaWm2, diffusePoa + groundPoa) : ctx.poaWm2;
        }
        else { poaEff = directPoa + diffusePoa + groundPoa; }
    }
    let pStc = Math.max(0, poaEff / 1000);
    if (ctx && isFinite(ctx.airTempC ?? NaN))
    {
        const tCell = cellTemperatureC(ctx.airTempC, poaEff, ctx.windMs ?? 0);
        pStc *= thermalDerating(tCell);
    }
    return Math.max(0, Math.min(100, pStc * 100));
}

// ---- scenarios -------------------------------------------------------------

const rows = [];
function add(y, mo, d, h, lat, lon, cloud, panel, ctx)
{
    const date = new Date(Date.UTC(y, mo - 1, d, h, 0, 0));
    const expected = computePvPower(date, lat, lon, cloud, panel || undefined, ctx || undefined);
    rows.push({ year: y, month: mo, day: d, hour: h, lat, lon, cloud, panel: panel || null, ctx: ctx || null, expected });
}

const SOUTH30 = { tiltDeg: 30, azimuthDeg: 180 };

// Night (below horizon).
add(2026, 6, 21, 0, 48.85, 2.35, 0, SOUTH30, null);
// Horizontal, no panel, clear and overcast.
add(2026, 6, 21, 12, 48.85, 2.35, 0, null, null);
add(2026, 6, 21, 12, 48.85, 2.35, 100, null, null);
// Horizontal shaded.
add(2026, 6, 21, 12, 48.85, 2.35, 20, null, { shading: true });
// Tilted south, clouds 0/40/100, no ctx.
for (const c of [0, 40, 100]) { add(2026, 6, 21, 12, 48.85, 2.35, c, SOUTH30, null); }
// Tilted with GHI supplied.
add(2026, 6, 21, 12, 48.85, 2.35, 30, SOUTH30, { ghiWm2: 600 });
// Tilted with direct + diffuse split.
add(2026, 6, 21, 12, 48.85, 2.35, 30, SOUTH30, { ghiWm2: 650, directWm2: 400, diffuseWm2: 150 });
// Tilted with GTI poa supplied, unshaded and shaded.
add(2026, 6, 21, 12, 48.85, 2.35, 30, SOUTH30, { poaWm2: 720 });
add(2026, 6, 21, 12, 48.85, 2.35, 30, SOUTH30, { poaWm2: 720, ghiWm2: 650, directWm2: 400, diffuseWm2: 150, shading: true });
// Thermal derate (hot) and cold gain.
add(2026, 6, 21, 12, 48.85, 2.35, 10, SOUTH30, { ghiWm2: 850, directWm2: 600, diffuseWm2: 120, airTempC: 32, windMs: 1.5 });
add(2026, 12, 21, 12, 48.85, 2.35, 10, SOUTH30, { ghiWm2: 300, directWm2: 180, diffuseWm2: 60, airTempC: -5, windMs: 4 });
// Trackers at a few times.
for (const h of [8, 12, 16]) {
    add(2026, 6, 21, h, 48.85, 2.35, 10, { tiltDeg: 30, azimuthDeg: 180, tracker: 'dual-axis' }, { ghiWm2: 700 });
    add(2026, 6, 21, h, 48.85, 2.35, 10, { tiltDeg: 30, azimuthDeg: 180, tracker: 'single-axis-h' }, { ghiWm2: 700 });
    add(2026, 6, 21, h, 48.85, 2.35, 10, { tiltDeg: 30, azimuthDeg: 180, tracker: 'single-axis-v' }, { ghiWm2: 700 });
}
// Full-ctx sweep across the day, two homes (N + S hemisphere), two seasons.
const homes = [[48.8566, 2.3522], [-27.47, 153.02]];
const days  = [[2026, 6, 21], [2026, 12, 21]];
for (const [lat, lon] of homes) {
    for (const [y, mo, d] of days) {
        for (const h of [6, 9, 12, 15, 18]) {
            add(y, mo, d, h, lat, lon, 35, SOUTH30,
                { ghiWm2: 500, directWm2: 320, diffuseWm2: 140, airTempC: 18, windMs: 3, poaWm2: 560 });
        }
    }
}

process.stdout.write(JSON.stringify(rows, null, 2) + "\n");
