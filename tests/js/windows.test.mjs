// Unit tests for web/windows.js, the per-city window list helpers (migration 018).
// Run with `node --test tests/js/*.test.mjs`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/windows.js", import.meta.url), "utf8");
const ctx = {};
vm.runInNewContext(src, ctx);
const {
  findWindow, pickWindow, windowBuiltAt, windowNote,
  addDays, yearsBefore, spanDays, defaultRange, clampRange, effectiveEnd,
  presetFor, presetRange, rangeNote,
} = ctx;

const w = (id, span, extra = {}) => ({
  id,
  label: id,
  span_days: span,
  partial: false,
  safety: true,
  hourly: id === "last_1y",
  res10: id === "last_1y" || id === "last_2y",
  caveats: [],
  data_start: "2020-01-01",
  ...extra,
});

const CHICAGO = [
  w("last_3m", 92), w("last_6m", 184), w("last_9m", 276),
  w("last_1y", 365), w("last_2y", 730), w("last_10y", 3652), w("last_20y", 7305),
];
const AUSTIN = [
  w("last_3m", 92), w("last_6m", 184), w("last_9m", 276),
  w("last_1y", 365), w("last_2y", 730), w("last_5y", 1826),
];
const LEGACY = [w("last_90d", 90), w("last_12m", 365), w("last_24m", 730)];

test("legacy names resolve both ways", () => {
  assert.equal(findWindow(CHICAGO, "last_12m").id, "last_1y");
  assert.equal(findWindow(LEGACY, "last_1y").id, "last_12m");
  assert.equal(findWindow(CHICAGO, "last_99y"), null);
  assert.equal(findWindow(CHICAGO, "last_30d").id, "last_3m");
  assert.equal(findWindow(LEGACY, "last_3m").id, "last_90d");
  assert.equal(findWindow(null, "last_1y"), null);
});

test("the current window is kept when the new city has it", () => {
  assert.equal(pickWindow(AUSTIN, "last_2y", 730, "last_1y").id, "last_2y");
});

test("a longer window falls back to the new city's longest shorter one", () => {
  assert.equal(pickWindow(AUSTIN, "last_20y", 7305, "last_1y").id, "last_5y");
  assert.equal(pickWindow(AUSTIN, "last_10y", 3652, "last_1y").id, "last_5y");
});

test("no span known falls back to the default, then the first", () => {
  assert.equal(pickWindow(AUSTIN, "last_20y", undefined, "last_1y").id, "last_1y");
  assert.equal(pickWindow([w("last_3m", 92)], "last_20y", undefined, "last_1y").id, "last_3m");
  assert.equal(pickWindow([], "last_1y", 365, "last_1y"), null);
});

test("res 10 is built only where the window says so", () => {
  assert.equal(windowBuiltAt(w("last_3m", 92), 10), false);
  assert.equal(windowBuiltAt(w("last_2y", 730), 10), true);
  assert.equal(windowBuiltAt(w("last_3m", 92), 8), true);
});

test("the note says partial, counts-only and caveats", () => {
  assert.equal(windowNote(w("last_1y", 365), 8), "");
  const old = w("last_20y", 7305, {
    partial: true,
    data_start: "2008-01-01",
    safety: false,
    caveats: ["Seattle records before May 2019 ..."],
  });
  const note = windowNote(old, 8);
  assert.match(note, /Data from Jan 2008\./);
  assert.match(note, /Counts only/);
  assert.match(note, /Seattle records before May 2019/);
  assert.match(windowNote(w("last_3m", 92), 10), /Not built at this cell size/);
});

// ---------------------------------------------------------------- date ranges

const win = (id, start, end, extra = {}) => ({
  ...w(id, spanDays({ from: start, to: end }), extra),
  start,
  end,
});
// Objects from the vm context have that realm's Object.prototype, which
// deepStrictEqual counts as a difference; compare plain copies.
const plain = (o) => ({ ...o });

const PHL = {
  coverage_end: "2026-10-02",
  selectable_start: "2006-01-01",
  custom_range_resolutions: [8, 9],
  series_caveats: [],
  windows: [
    win("last_3m", "2026-07-03", "2026-10-02"),
    win("last_1y", "2025-10-03", "2026-10-02", { hourly: true, res10: true }),
  ],
};

test("date arithmetic stays on calendar days", () => {
  assert.equal(addDays("2026-03-08", 1), "2026-03-09"); // a US DST change
  assert.equal(addDays("2025-12-31", 1), "2026-01-01");
  assert.equal(yearsBefore("2024-02-29", 1), "2023-02-28");
  assert.equal(spanDays({ from: "2026-10-09", to: "2026-10-09" }), 1);
  assert.equal(spanDays({ from: "2025-10-10", to: "2026-10-09" }), 365);
});

test("the default range is the year up to today, like last_1y", () => {
  const range = defaultRange("2026-10-09");
  assert.equal(range.from, "2025-10-10");
  assert.equal(range.to, "2026-10-09");
});

test("clamping orders the dates and keeps them inside the city's bounds", () => {
  assert.deepEqual(plain(clampRange({ from: "2026-05-01", to: "2026-04-01" }, null, null)), {
    from: "2026-04-01", to: "2026-05-01",
  });
  assert.deepEqual(plain(clampRange({ from: "1999-01-01", to: "2030-01-01" }, "2006-01-01", "2026-10-09")), {
    from: "2006-01-01", to: "2026-10-09",
  });
  const day = { from: "2026-02-03", to: "2026-02-03" };
  assert.deepEqual(plain(clampRange(day, "2006-01-01", "2026-10-09")), day);
});

test("the end stops where the data does, never before the start", () => {
  assert.equal(effectiveEnd({ from: "2026-01-01", to: "2026-10-09" }, "2026-10-02"), "2026-10-02");
  assert.equal(effectiveEnd({ from: "2026-10-05", to: "2026-10-09" }, "2026-10-02"), "2026-10-05");
  assert.equal(effectiveEnd({ from: "2026-01-01", to: "2026-02-01" }, "2026-10-02"), "2026-02-01");
});

test("a preset is matched through the end clamp, and only exactly", () => {
  const filled = presetRange(PHL.windows[1], "2026-10-09");
  assert.deepEqual(plain(filled), { from: "2025-10-03", to: "2026-10-09" });
  assert.equal(presetFor(filled, PHL.windows, PHL.coverage_end).id, "last_1y");
  assert.equal(presetFor({ from: "2025-10-03", to: "2026-10-02" }, PHL.windows, PHL.coverage_end).id, "last_1y");
  assert.equal(presetFor({ from: "2025-10-04", to: "2026-10-09" }, PHL.windows, PHL.coverage_end), null);
  assert.equal(presetFor(defaultRange("2026-10-09"), PHL.windows, PHL.coverage_end), null);
});

test("the range note says where the data stops, and what a range lacks", () => {
  const today = "2026-10-09";
  assert.match(rangeNote(defaultRange(today), PHL, 8), /Data reported through Oct 2, 2026\./);
  assert.equal(rangeNote({ from: "2025-01-01", to: "2025-12-31" }, PHL, 8), "");
  assert.match(rangeNote({ from: "2026-02-03", to: "2026-02-03" }, PHL, 8), /few incidents per cell/);
  assert.match(rangeNote({ from: "2025-01-01", to: "2025-12-31" }, PHL, 10), /not served at this cell size/);
  // A preset says what its stored window says.
  assert.match(rangeNote({ from: "2026-07-03", to: today }, PHL, 10), /Not built at this cell size/);
});

test("a caveat shows only for a range that reaches into its period", () => {
  const SEA = {
    ...PHL,
    series_caveats: [{ from: "2008-01-01", to: "2019-05-01", text: "Seattle records before May 2019 ..." }],
  };
  assert.match(rangeNote({ from: "2018-01-01", to: "2020-01-01" }, SEA, 8), /Seattle records/);
  assert.doesNotMatch(rangeNote({ from: "2019-05-01", to: "2020-01-01" }, SEA, 8), /Seattle records/);
});
