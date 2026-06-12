// Golden-fixture generator for the solar position parity test.
//
// Mirrors the Helios card's getSunPosition (src/engine/sun.ts) so the Python
// port in custom_components/helios_forecast/solar/geometry.py can be checked to
// produce the exact same altitude / azimuth. Run from the repo root:
//
//   node tests/fixtures/_gen_sun_position.mjs > tests/fixtures/sun_position.json
//
// The cache in the card is a render-loop optimisation only; it is omitted here.

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
    const cAz  = cAlt > 1e-4
        ? (Math.sin(D * decl) - Math.sin(D * lat) * sinA) / (Math.cos(D * lat) * cAlt)
        : 0;
    let az = Math.acos(Math.max(-1, Math.min(1, cAz))) / D;
    if (ha > 0)
    {
        az = 360 - az;
    }
    return { altitude: alt, azimuth: az };
}

// (name, lat, lon) homes spread across both hemispheres and far from Greenwich.
const homes = [
    ["paris",     48.8566,   2.3522],
    ["brisbane", -27.4700, 153.0200],
    ["nyc",       40.7128, -74.0060],
    ["tokyo",     35.6800, 139.7000],
    ["sydney",   -33.8700, 151.2100],
    ["equator",    0.0000,   0.0000],
    ["oslo",      59.9100,  10.7500],
];

// Solstices + equinoxes, and four times of day in UTC.
const days  = [[2026, 3, 20], [2026, 6, 21], [2026, 9, 22], [2026, 12, 21]];
const hours = [0, 6, 12, 18];

const out = [];
for (const [name, lat, lon] of homes)
{
    for (const [y, mo, d] of days)
    {
        for (const h of hours)
        {
            const date = new Date(Date.UTC(y, mo - 1, d, h, 0, 0));
            const { altitude, azimuth } = getSunPosition(date, lat, lon);
            out.push({ home: name, year: y, month: mo, day: d, hour: h, lat, lon, altitude, azimuth });
        }
    }
}

process.stdout.write(JSON.stringify(out, null, 2) + "\n");
