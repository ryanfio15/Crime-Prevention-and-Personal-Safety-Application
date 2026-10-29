// Unit tests for web/directions.js, and the parity check between the map's
// cellRatio (web/app.js) and the server's scoring.cell_ratio over the shared
// cases in tests/fixtures/routing/ratio_cases.json.
// Run with `node --test tests/js/*.test.mjs`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/directions.js", import.meta.url), "utf8");
const ctx = { Intl, Date, Math, Number, String, setTimeout, clearTimeout };
vm.runInNewContext(src, ctx);
const {
  formatDuration, formatDistance, activitySentence, pickReason, routeCardModel,
  safeHex, textOn, transitLegSummary, latLngFromString, routesBbox, segmentFeatures,
  routeLineFeatures, altFeatures, directionsKey, clockIn, debounce,
  navSteps, projectOnLine, navProgress, headingFromOrientation,
} = ctx;
const plain = (v) => JSON.parse(JSON.stringify(v));

test("formatDuration", () => {
  assert.equal(formatDuration(20), "<1 min");
  assert.equal(formatDuration(12 * 60), "12 min");
  assert.equal(formatDuration(65 * 60), "1 h 5 min");
  assert.equal(formatDuration(120 * 60), "2 h");
  assert.equal(formatDuration(NaN), "—");
});

test("formatDistance", () => {
  assert.equal(formatDistance(3), "10 m");
  assert.equal(formatDistance(847), "850 m");
  assert.equal(formatDistance(1234), "1.2 km");
  assert.equal(formatDistance(14_400), "14 km");
});

const comparable = (mean, extra = {}) => ({
  comparable: true, mean_ratio: mean, peak_ratio: mean, rated_share: 1,
  sense: mean > 1.05 ? "higher" : mean < 0.95 ? "lower" : "about", ...extra,
});

test("activitySentence is relative to the city, never 'safe'", () => {
  const low = activitySentence(comparable(0.62), "violent");
  assert.equal(low.headline, "Reported violent activity lower than the city average");
  assert.match(low.detail, /^62% of the city average along the route$/);
  const about = activitySentence(comparable(1.02), "non-violent");
  assert.match(about.headline, /about the city average/);
  for (const s of [low, about]) assert.doesNotMatch(`${s.headline} ${s.detail}`, /\bsafe|danger/i);
});

test("activitySentence names a hot stretch and partial coverage", () => {
  const s = activitySentence(comparable(1.3, { peak_ratio: 2.4, rated_share: 0.8 }), "violent");
  assert.match(s.detail, /one stretch reaches 240%/);
  assert.match(s.detail, /80% of the route is rated/);
});

test("activitySentence claims nothing when the route is not comparable", () => {
  const s = activitySentence({ comparable: false, rated_share: 0.3 }, "violent");
  assert.equal(s.headline, "Not enough data to compare");
});

test("pickReason", () => {
  assert.equal(pickReason({ lower: 2 }, "walk"), null);
  assert.match(pickReason({ lower: null, why: "too_slow", extra_allowed_s: 360 }, "walk"), /more than 6 min longer/);
  assert.match(pickReason({ lower: null, why: "not_lower", extra_allowed_s: 180 }, "drive"), /within 3 min/);
  assert.match(pickReason({ lower: null, why: "single_route" }, "transit"), /transit option/);
});

const route = (d, activity) => ({ duration_s: d, distance_m: 1000, activity: { violent: activity } });

test("routeCardModel puts the safest, then the fastest, then the rest", () => {
  const result = {
    routes: [route(600, comparable(1.2)), route(660, comparable(1.1)), route(700, comparable(0.8))],
    picks: { violent: { fastest: 0, lower: null } },
  };
  const cards = plain(routeCardModel(result, "violent"));
  assert.deepEqual(cards.map((c) => c.index), [2, 0, 1]);
  assert.deepEqual(cards.map((c) => c.role), ["Safest", "Fastest", "Alternative"]);
  assert.equal(cards[0].extra, "+2 min");
  assert.equal(cards[1].extra, null);
});

test("routeCardModel calls a fastest route that is also the safest both", () => {
  const result = {
    routes: [route(600, comparable(0.9)), route(660, comparable(1.1)), route(700, comparable(0.9))],
    picks: { violent: { fastest: 0, lower: null } },
  };
  const cards = plain(routeCardModel(result, "violent"));
  // A tie on activity goes to the quicker route.
  assert.deepEqual(cards.map((c) => c.index), [0, 1, 2]);
  assert.deepEqual(cards.map((c) => c.role), ["Fastest and safest", "Alternative", "Alternative"]);
});

test("routeCardModel names no safest route without two comparable ones", () => {
  const unrated = { comparable: false, rated_share: 0.3 };
  const result = {
    routes: [route(600, unrated), route(660, comparable(0.5)), route(700, unrated)],
    picks: { violent: { fastest: 0, lower: null } },
  };
  const cards = plain(routeCardModel(result, "violent"));
  assert.deepEqual(cards.map((c) => c.index), [0, 1, 2]);
  assert.deepEqual(cards.map((c) => c.role), ["Fastest", "Alternative", "Alternative"]);
});

test("safeHex accepts GTFS colours and rejects anything else", () => {
  assert.equal(safeHex("BF0D3E"), "#bf0d3e");
  assert.equal(safeHex("#00a1de"), "#00a1de");
  for (const bad of ["red", "#fff", "12345g", "#123456;background:url(x)", null, 7]) {
    assert.equal(safeHex(bad), null, String(bad));
  }
});

test("textOn picks the readable ink", () => {
  assert.equal(textOn("#ffd200"), "#0b0b0b");
  assert.equal(textOn("#003da5"), "#ffffff");
  assert.equal(textOn("nonsense"), "#ffffff");
});

test("transitLegSummary", () => {
  const walk = transitLegSummary({ mode: "WALK", transit: false, duration_s: 240, distance_m: 300 });
  assert.equal(walk.text, "Walk 4 min · 300 m");
  const ride = transitLegSummary({
    mode: "SUBWAY", transit: true, duration_s: 600, route_name: "Red", headsign: "Shady Grove",
    color: "BF0D3E", text_color: null, num_stops: 4,
  });
  assert.equal(ride.name, "Red");
  assert.equal(ride.color, "#bf0d3e");
  assert.equal(ride.textColor, "#ffffff");
  assert.equal(ride.text, "toward Shady Grove");
  assert.equal(ride.detail, "10 min · 4 stops");
  // A hostile colour never reaches a style attribute.
  const bad = transitLegSummary({ mode: "BUS", transit: true, duration_s: 60, color: "x;y", text_color: "z" });
  assert.equal(bad.color, null);
  assert.equal(bad.textColor, null);
});

test("latLngFromString", () => {
  assert.deepEqual(plain(latLngFromString("39.9526, -75.1652")), { lat: 39.9526, lng: -75.1652 });
  assert.deepEqual(plain(latLngFromString("(39.95 -75.16)")), { lat: 39.95, lng: -75.16 });
  assert.equal(latLngFromString("1100 Congress Ave"), null);
  assert.equal(latLngFromString("95, 10"), null);
});

const line = (coords) => ({ type: "LineString", coordinates: coords });

test("routesBbox spans every route", () => {
  const box = routesBbox([
    { geometry: line([[-75.2, 39.9], [-75.1, 39.95]]) },
    { geometry: line([[-75.25, 39.92]]) },
  ]);
  assert.deepEqual(plain(box), [[-75.25, 39.9], [-75.1, 39.95]]);
  assert.equal(routesBbox([]), null);
});

test("segmentFeatures marks unrated stretches", () => {
  const fc = plain(segmentFeatures({ segments: [
    { ratio: 0.8, coordinates: [[0, 0], [1, 1]] },
    { ratio: null, coordinates: [[1, 1], [2, 2]] },
  ] }));
  assert.deepEqual(fc.features.map((f) => f.properties.ratio), [0.8, -1]);
  assert.equal(segmentFeatures(null).features.length, 0);
});

test("routeLineFeatures splits a transit route by leg", () => {
  const route = {
    mode: "transit", geometry: line([[0, 0], [2, 2]]),
    legs: [
      { transit: false, geometry: line([[0, 0], [1, 1]]) },
      { transit: true, color: "BF0D3E", geometry: line([[1, 1], [2, 2]]) },
    ],
  };
  const fc = plain(routeLineFeatures(route));
  assert.deepEqual(fc.features.map((f) => f.properties), [
    { walk: true, color: null }, { walk: false, color: "#bf0d3e" },
  ]);
  // A walking route is one solid line; only a transit trip's walks are dotted.
  assert.equal(plain(routeLineFeatures({ mode: "walk", geometry: line([]) })).features[0].properties.walk, false);
  assert.equal(plain(altFeatures([{ geometry: line([]) }, { geometry: line([]) }], 0)).features.length, 1);
});

test("directionsKey", () => {
  const a = directionsKey({ from: { lat: 1, lng: 2 }, to: { lat: 3, lng: 4 }, mode: "walk" });
  const b = directionsKey({ from: { lat: 1.000001, lng: 2 }, to: { lat: 3, lng: 4 }, mode: "walk" });
  const c = directionsKey({ from: { lat: 1, lng: 2 }, to: { lat: 3, lng: 4 }, mode: "bike" });
  assert.equal(a, b);
  assert.notEqual(a, c);
});

test("clockIn reads the city's zone", () => {
  const label = clockIn("2026-10-29T21:30:00Z", "America/Los_Angeles").replace(/\s/g, " ");
  assert.equal(label, "2:30 PM");
  assert.equal(clockIn("not a time", "UTC"), "");
});

test("debounce runs once after the quiet period", async () => {
  let calls = 0;
  const fn = debounce(() => calls++, 10);
  fn(); fn(); fn();
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(calls, 1);
});

// ---------------------------------------------------------------- parity

// cellRatio is lifted out of app.js as written, so a change there that the
// server's port does not follow fails here.
const app = readFileSync(new URL("../../web/app.js", import.meta.url), "utf8");
const match = app.match(/function cellRatio\([\s\S]*?\n}\n/);
assert.ok(match, "cellRatio not found in web/app.js");
const appCtx = {};
vm.runInNewContext(`${match[0]}; this.cellRatio = cellRatio;`, appCtx);

const cases = JSON.parse(
  readFileSync(new URL("../fixtures/routing/ratio_cases.json", import.meta.url), "utf8")
).cases;

for (const c of cases) {
  test(`cellRatio parity: ${c.name}`, () => {
    // A cell with no row is no feature on the map: nothing to colour.
    if (c.cell === null) {
      assert.equal(c.expected, null);
      return;
    }
    const hourly = c.hour !== null;
    const measure = {
      valueProp: hourly ? "hsm" : "sm",
      isPainted: hourly ? (p) => (p.htier ?? 0) !== 0 : (p) => (p.tier ?? 0) !== 0,
      fallback: { valueProp: "sm", tier: "tier" },
    };
    const own = c.own ? { average: c.own } : null;
    const overall = (hourly ? c.overall : c.own) ? { average: hourly ? c.overall : c.own } : null;
    const domain = own || overall ? { own, overall } : null;
    const got = appCtx.cellRatio(c.cell, measure, domain);
    if (c.expected === null) assert.equal(got, null);
    else assert.ok(Math.abs(got - c.expected) < 1e-9, `${got} != ${c.expected}`);
  });
}


// ------------------------------------------------------------- navigation

// A street running due east along lat 40 from lng -75.000 to -74.990
// (~850 m), then north for ~555 m: two maneuvers and an arrival.
const EAST = [[-75.0, 40.0], [-74.995, 40.0], [-74.99, 40.0]];
const NORTH = [[-74.99, 40.0], [-74.99, 40.0025], [-74.99, 40.005]];
const streetRoute = {
  mode: "walk",
  geometry: { type: "LineString", coordinates: [...EAST, ...NORTH.slice(1)] },
  steps: [
    { instruction: "Walk east on Main Street.", distance_m: 850, street_names: ["Main Street"], begin_index: 0, end_index: 2 },
    { instruction: "Turn left onto Oak Avenue.", distance_m: 555, street_names: ["Oak Avenue"], begin_index: 2, end_index: 4 },
    { instruction: "You have arrived.", distance_m: 0, street_names: [], begin_index: 4, end_index: 4 },
  ],
};

test("navSteps gives each maneuver the stretch it covers", () => {
  const steps = plain(navSteps(streetRoute, "America/New_York"));
  assert.equal(steps.length, 3);
  assert.deepEqual(steps[0].points, EAST);
  assert.deepEqual(steps[1].points, NORTH);
  assert.equal(steps[1].street, "Oak Avenue");
  assert.equal(steps[2].points.length, 1);
});

test("navSteps turns transit legs into walk and ride steps", () => {
  const route = {
    mode: "transit",
    geometry: { type: "LineString", coordinates: [] },
    legs: [
      { mode: "WALK", transit: false, duration_s: 240, distance_m: 300,
        destination: { name: "Dupont Circle" }, geometry: { coordinates: EAST },
        end_time: "2026-11-02T22:04:00Z" },
      { mode: "SUBWAY", transit: true, duration_s: 600, route_name: "Red", headsign: "Glenmont",
        origin: { name: "Dupont Circle" }, destination: { name: "Union Station" }, num_stops: 4,
        start_time: "2026-11-02T22:06:00Z", end_time: "2026-11-02T22:16:00Z",
        geometry: { coordinates: NORTH } },
    ],
  };
  const [walk, ride] = plain(navSteps(route, "America/New_York"));
  assert.equal(walk.text, "Walk to Dupont Circle");
  assert.equal(walk.endsAt, Date.parse("2026-11-02T22:04:00Z"));
  assert.equal(ride.text, "Take Red toward Glenmont");
  assert.match(ride.sub, /Board at Dupont Circle · 5:06 PM · get off at Union Station · 10 min · 4 stops/);
  assert.equal(ride.chip.name, "Red");
});

test("projectOnLine measures off-line and along-line distance", () => {
  // 50 m south of the middle of the eastward street.
  const at = projectOnLine(EAST, { lat: 40.0 - 50 / 111320, lng: -74.995 });
  assert.ok(Math.abs(at.dist - 50) < 0.5, at.dist);
  assert.ok(Math.abs(at.along - at.total / 2) < 1, at.along);
  assert.equal(projectOnLine([], { lat: 40, lng: -75 }), null);
});

test("navProgress follows the route forward and never back", () => {
  const steps = navSteps(streetRoute, "UTC");
  // A third of the way along Main Street.
  let p = navProgress(steps, { lat: 40.0, lng: -74.9967 });
  assert.equal(p.index, 0);
  assert.ok(p.offRoute < 1 && p.remaining > 500 && p.remaining < 600, p.remaining);
  // Up Oak Avenue: the first step is behind.
  p = navProgress(steps, { lat: 40.002, lng: -74.99 }, 0);
  assert.equal(p.index, 1);
  // Back on Main Street again: still on step 1, now well off it.
  p = navProgress(steps, { lat: 40.0, lng: -74.9967 }, 1);
  assert.equal(p.index, 1);
  assert.ok(p.offRoute > 200);
  // Within a few metres of the corner counts as having turned.
  p = navProgress(steps, { lat: 40.0, lng: -74.99005 }, 0);
  assert.equal(p.index, 1);
  // At the end: arrived.
  p = navProgress(steps, { lat: 40.005, lng: -74.99 }, 1);
  assert.equal(p.index, steps.length);
});

test("navProgress passes a transit leg once its time is up", () => {
  const steps = [
    { points: EAST, endsAt: Date.parse("2026-11-02T22:04:00Z") },
    { points: NORTH, endsAt: Date.parse("2026-11-02T22:16:00Z") },
  ];
  // Underground, with a stale fix back at the start of the walk.
  const p = navProgress(steps, { lat: 40.0, lng: -75.0 }, 0, Date.parse("2026-11-02T22:10:00Z"));
  assert.equal(p.index, 1);
});

test("headingFromOrientation reads iOS and absolute events", () => {
  assert.equal(headingFromOrientation({ webkitCompassHeading: 90 }), 90);
  assert.equal(headingFromOrientation({ absolute: true, alpha: 90 }), 270);
  // Phone turned to landscape: the screen's own rotation is added back.
  assert.equal(headingFromOrientation({ absolute: true, alpha: 90 }, 90), 0);
  assert.equal(headingFromOrientation({ absolute: false, alpha: 90 }), null);
  assert.equal(headingFromOrientation({}), null);
});
