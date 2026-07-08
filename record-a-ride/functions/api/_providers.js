// Per-city upstream config for the live-data proxy. Keeps the secret env-var
// name, base URL, and endpoint allow-list out of the route handler so adding a
// city is a data change, not a code change. The matching client lives in
// providers/<city>.js and hits /api/<city>/<endpoint>.

export const UPSTREAMS = {
  cta: {
    base: "https://www.ctabustracker.com/bustime/api/v2",
    keyEnv: "CTA_KEY",
    keyParam: "key",
    allowed: new Set(["getpredictions", "getvehicles", "getstops", "getroutes"]),
    extra: { format: "json" },
    cacheTtl: 10,
  },
  mta: {
    base: "https://bustime.mta.info/api/siri",
    keyEnv: "MTA_KEY",
    keyParam: "key",
    allowed: new Set(["vehicle-monitoring.json", "stop-monitoring.json"]),
    extra: {},
    cacheTtl: 10,
  },
};
