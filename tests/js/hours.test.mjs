// Unit tests for web/hours.js, the city clock and this-hour-vs-usual reading.
// Run with `node --test tests/js/*.test.mjs`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/hours.js", import.meta.url), "utf8");
const ctx = { Intl, Date };
vm.runInNewContext(src, ctx);
const { cityHour, cityTimeLabel, hourVsUsual } = ctx;

const flat = (n) => Array.from({ length: 24 }, () => n);

test("cityHour reads the city's zone, not the browser's", () => {
  // 2026-07-01 02:30 UTC is 22:30 the previous day in New York (EDT) and
  // 19:30 in Los Angeles (PDT).
  const at = new Date(Date.UTC(2026, 6, 1, 2, 30));
  assert.equal(cityHour("America/New_York", at), 22);
  assert.equal(cityHour("America/Los_Angeles", at), 19);
  assert.equal(cityHour("UTC", at), 2);
});

test("cityHour falls back to the browser clock for an unknown zone", () => {
  const at = new Date(Date.UTC(2026, 0, 1, 12));
  assert.equal(cityHour("Not/AZone", at), at.getHours());
  assert.equal(cityHour(null, at), at.getHours());
});

test("cityTimeLabel shows the city's exact time with minutes", () => {
  const at = new Date(Date.UTC(2026, 9, 16, 2, 13));
  // Normalise the narrow no-break space some ICU versions put before AM/PM.
  const label = cityTimeLabel("America/New_York", at).replace(/\s/g, " ");
  assert.equal(label, "10:13 PM");
});

test("an hour well above the daily average reads as up", () => {
  const counts = flat(10);
  counts[22] = 20;
  const r = hourVsUsual(counts, 22);
  assert.equal(r.direction, "up");
  // Average is 250/24; 20 against that is +92%.
  assert.equal(Math.round(r.change * 100), 92);
});

test("an hour well below the daily average reads as down", () => {
  const counts = flat(10);
  counts[4] = 2;
  assert.equal(hourVsUsual(counts, 4).direction, "down");
});

test("small differences inside the deadband read as flat", () => {
  const counts = flat(100);
  counts[9] = 105;
  assert.equal(hourVsUsual(counts, 9).direction, "flat");
});

test("too little evidence returns null", () => {
  assert.equal(hourVsUsual(flat(1), 5), null);
});
