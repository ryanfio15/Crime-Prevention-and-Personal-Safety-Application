// Unit tests for web/hours.js, the city clock and this-hour-vs-usual reading.
// Run with `node --test tests/js/*.test.mjs`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/hours.js", import.meta.url), "utf8");
const ctx = { Intl, Date };
vm.runInNewContext(src, ctx);
const {
  cityHour, cityTimeLabel, hourVsUsual, hourOutlook, quietestWindow, hourName,
  cityDay, dayName, dayVsUsual,
} = ctx;
// Arrays built inside the vm context have that context's prototype, which
// deepStrictEqual treats as different; copy into this realm before comparing.
const plain = (a) => [...a];

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

test("cityTimeLabel converts one UTC instant to each city's clock and zone", () => {
  const at = new Date(Date.UTC(2026, 9, 16, 2, 13));
  const label = (zone) => cityTimeLabel(zone, at, { zoneName: true }).replace(/\s/g, " ");
  assert.equal(label("America/New_York"), "10:13 PM EDT");
  assert.equal(label("America/Chicago"), "9:13 PM CDT");
  assert.equal(label("America/Los_Angeles"), "7:13 PM PDT");
  // After the November change the same wall-clock math uses standard time.
  const winter = new Date(Date.UTC(2026, 11, 16, 3, 13));
  assert.equal(cityTimeLabel("America/New_York", winter, { zoneName: true }).replace(/\s/g, " "),
    "10:13 PM EST");
});

test("cityTimeLabel falls back to the browser clock for an unknown zone", () => {
  assert.match(cityTimeLabel("Not/AZone", new Date(), { zoneName: true }), /\d:\d\d/);
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
  assert.equal(hourOutlook(flat(1), 5), null);
  assert.equal(quietestWindow(flat(1), 5), null);
});

test("the outlook rises when the next three hours are busier", () => {
  const counts = flat(10);
  counts[19] = counts[20] = counts[21] = 20;
  const o = hourOutlook(counts, 18);
  assert.equal(o.direction, "up");
  assert.equal(Math.round(o.change * 100), 100);
});

test("the outlook wraps past midnight and can fall", () => {
  const counts = flat(10);
  counts[23] = 30;
  assert.equal(hourOutlook(counts, 23).direction, "down");
});

test("the quietest window is the lowest three-hour stretch within twelve hours", () => {
  const counts = flat(100);
  // Quiet at 02:00-05:00, quieter still at 14:00 -- but 14:00 is out of reach
  // from 22:00 within twelve hours.
  counts[2] = counts[3] = counts[4] = 20;
  counts[14] = counts[15] = counts[16] = 5;
  const w = quietestWindow(counts, 22);
  assert.equal(w.start, 2);
  assert.equal(w.end, 5);
  assert.equal(w.hoursAway, 4);
  assert.equal(w.startsNow, false);
  assert.deepEqual(plain(w.blocks), [2, 3, 4]);
  assert.ok(w.vsAverage < 0);
});

test("a daytime window is compared with now, and flagged when still above average", () => {
  // The 9 AM case: the next twelve hours are all busier than the day's
  // average, but 1 PM-4 PM is still clearly quieter than now.
  const counts = flat(40);
  for (let h = 9; h <= 21; h++) counts[h] = 100;
  counts[9] = 120;
  counts[13] = counts[14] = counts[15] = 80;
  const w = quietestWindow(counts, 9);
  assert.equal(w.start, 13);
  assert.equal(w.end, 16);
  assert.equal(Math.round(w.vsNow * 100), -33);
  assert.equal(w.quieter, true);
  assert.equal(w.aboveAverage, true);
});

test("no window counts as quieter when nothing ahead is meaningfully below now", () => {
  const counts = flat(100);
  counts[10] = 95;
  const w = quietestWindow(counts, 9);
  assert.equal(w.quieter, false);
});

test("the quietest window can be the stretch starting now", () => {
  const counts = flat(100);
  counts[3] = counts[4] = counts[5] = 10;
  const w = quietestWindow(counts, 3);
  assert.equal(w.startsNow, true);
  assert.equal(w.start, 3);
});

test("hourName reads as the clock does", () => {
  assert.equal(hourName(0), "12 AM");
  assert.equal(hourName(3), "3 AM");
  assert.equal(hourName(12), "12 PM");
  assert.equal(hourName(22), "10 PM");
  assert.equal(hourName(24), "12 AM");
});


test("cityDay reads the city's zone, Monday first", () => {
  // 2026-07-04 is a Saturday. 02:30 UTC on the 5th is still Saturday evening
  // in New York and Los Angeles, and already Sunday in UTC.
  const at = new Date(Date.UTC(2026, 6, 5, 2, 30));
  assert.equal(cityDay("America/New_York", at), 5);
  assert.equal(cityDay("America/Los_Angeles", at), 5);
  assert.equal(cityDay("UTC", at), 6);
  assert.equal(dayName(5), "Saturday");
  assert.equal(dayName(0), "Monday");
});

test("cityDay falls back to the browser's weekday for an unknown zone", () => {
  const at = new Date(Date.UTC(2026, 0, 5, 12)); // a Monday
  assert.equal(cityDay("Not/AZone", at), (at.getDay() + 6) % 7);
});

test("dayVsUsual reads a weekday against the average day", () => {
  const week = [10, 10, 10, 10, 10, 20, 10]; // a busy Saturday, 80 in all
  const sat = dayVsUsual(week, 5);
  assert.equal(sat.direction, "up");
  assert.ok(Math.abs(sat.change - (20 / (80 / 7) - 1)) < 1e-12);
  assert.equal(dayVsUsual(week, 0).direction, "down");
  assert.equal(dayVsUsual([5, 5, 5, 5, 5, 5, 5], 2).direction, "flat");
  // Too little evidence, a bad index, or the wrong shape: no reading.
  assert.equal(dayVsUsual([1, 1, 1, 1, 1, 1, 1], 0), null);
  assert.equal(dayVsUsual(week, 7), null);
  assert.equal(dayVsUsual(flat(10), 0), null);
});
