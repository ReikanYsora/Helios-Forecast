// Golden-fixture generator for sample_sky_residual.
//
// Mirrors the card's sampleSkyResidual (src/card/forecast-sky.ts) and emits both
// the constructed map and the query results, so the Python port reads the exact
// same map arrays and is checked against the same bilinear + confidence blend.
//
//   node tests/fixtures/_gen_sky_sample.mjs > tests/fixtures/sky_sample.json

const AZ_STEP_DEG = 10;
const ALT_STEP_DEG = 5;
const N_AZ = 36;
const N_ALT = 18;

function sampleSkyResidual(map, azDeg, altDeg)
{
    const g = map.globalRatio;
    if (altDeg <= 0) { return g; }
    let az = azDeg % 360;
    if (az < 0) { az += 360; }
    const alt = Math.max(0, Math.min(90 - 1e-3, altDeg));
    const fAz = az / AZ_STEP_DEG;
    const fAlt = alt / ALT_STEP_DEG;
    const az0 = Math.floor(fAz);
    const alt0 = Math.floor(fAlt);
    const dAz = fAz - az0;
    const dAlt = fAlt - alt0;
    let num = 0, den = 0;
    for (let i = 0; i <= 1; i++)
    {
        for (let j = 0; j <= 1; j++)
        {
            const ai = (az0 + i) % map.nAz;
            const aj = alt0 + j;
            if (aj < 0 || aj >= map.nAlt) { continue; }
            const idx = aj * map.nAz + ai;
            const cellM = map.conf[idx] * map.m[idx] + (1 - map.conf[idx]) * g;
            const w = (i === 0 ? 1 - dAz : dAz) * (j === 0 ? 1 - dAlt : dAlt);
            num += w * cellM;
            den += w;
        }
    }
    return den > 0 ? num / den : g;
}

const m = new Float32Array(N_AZ * N_ALT).fill(1.0);
const conf = new Float32Array(N_AZ * N_ALT);
// A confident shading dip at az 180 / alt 30 (idx 6*36+18 = 234) and a neighbour.
m[234] = 2.0; conf[234] = 0.8;
m[235] = 0.5; conf[235] = 0.9;   // az 190 / alt 30
m[198] = 1.7; conf[198] = 0.4;   // az 180 / alt 25 (idx 5*36+18)
const map = { nAz: N_AZ, nAlt: N_ALT, m, conf, globalRatio: 1.0 };

const queries = [
    [180, 30], [185, 32], [170, 28], [180, 27], [188, 31],
    [0, 45], [359, 10], [180, 5], [180, 0], [90, 60], [180, 89.5],
];

const out = {
    map: { nAz: N_AZ, nAlt: N_ALT, globalRatio: map.globalRatio, m: Array.from(m), conf: Array.from(conf) },
    queries: queries.map(([az, alt]) => ({ az, alt, expected: sampleSkyResidual(map, az, alt) })),
};
process.stdout.write(JSON.stringify(out) + "\n");
