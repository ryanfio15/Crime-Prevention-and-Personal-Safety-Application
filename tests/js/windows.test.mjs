// Unit tests for web/windows.js, the per-city window list helpers (migration 018).
// Run with `node --test tests/js/*.test.mjs`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/windows.js", import.meta.url), "utf8");
const ctx = {};
vm.runInNewContext(src, ctx);
const { findWindow, pickWindow, windowBuiltAt, windowNote } = ctx;

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
