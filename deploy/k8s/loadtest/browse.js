// What one visitor's browser does (web/app.js), as a k6 virtual user:
// load the city list, load a city's map layer, then sit on the page polling
// /version -- every 20 s here, three times faster than the real page, so a
// run of N users is a harsher load than N real tabs.
//
// Run by deploy/k8s/loadtest/run.sh as an in-cluster Job. BASE_URL defaults
// to the API Service (bypassing the ingress, whose per-address limits would
// otherwise throttle every virtual user as one client); VUS sets the peak.
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://safety-api.safety.svc";
const VUS = Number(__ENV.VUS || 3000);

export const options = {
  scenarios: {
    browse: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "3m", target: VUS },
        { duration: "5m", target: VUS },
        { duration: "1m", target: 0 },
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{name:cities}": ["p(95)<300"],
    "http_req_duration{name:cells}": ["p(95)<300"],
    "http_req_duration{name:version}": ["p(95)<300"],
  },
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

export function setup() {
  const response = http.get(`${BASE}/api/v1/cities`);
  const cities = (response.json("cities") || []).map((c) => c.source_id);
  if (cities.length === 0) throw new Error("no cities served yet -- has the bootstrap finished?");
  // Build each city's default layer once, so the run measures serving, not
  // the first build after a deploy.
  for (const city of cities) http.get(`${BASE}/api/v1/cells?city=${city}`);
  return { cities };
}

export default function (data) {
  const city = data.cities[Math.floor(Math.random() * data.cities.length)];
  const params = { headers: { "Accept-Encoding": "gzip" } };
  check(http.get(`${BASE}/api/v1/cities`, { ...params, tags: { name: "cities" } }), {
    "cities 200": (r) => r.status === 200,
  });
  check(
    http.get(`${BASE}/api/v1/cells?city=${city}&res=8&window=last_1y&category=all`, {
      ...params,
      tags: { name: "cells" },
    }),
    { "cells 200": (r) => r.status === 200 },
  );
  for (let i = 0; i < 3; i++) {
    sleep(20);
    check(http.get(`${BASE}/api/v1/version?city=${city}`, { ...params, tags: { name: "version" } }), {
      "version 200": (r) => r.status === 200,
    });
  }
}
