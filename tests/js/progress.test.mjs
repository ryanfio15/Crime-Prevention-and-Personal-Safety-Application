// Unit tests for web/progress.js, the first load's progress bar.
// Run with `node --test tests/js/*.test.mjs`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/progress.js", import.meta.url), "utf8");
const ctx = {};
vm.runInNewContext(src, ctx);
const { formatEta, progressView } = ctx;

test("formatEta reads in minutes, then hours and minutes", () => {
  assert.equal(formatEta(30), "about a minute left");
  assert.equal(formatEta(25 * 60), "about 25 min left");
  assert.equal(formatEta(70 * 60), "about 1 h 10 min left");
  assert.equal(formatEta(120 * 60), "about 2 h left");
});

test("formatEta has nothing to say without an estimate", () => {
  assert.equal(formatEta(null), null);
  assert.equal(formatEta(undefined), null);
  assert.equal(formatEta(-5), null);
  assert.equal(formatEta(Number.NaN), null);
});

test("nothing loading hides the bar", () => {
  assert.equal(progressView({ active: false, cities: [] }).show, false);
  assert.equal(progressView(null).show, false);
});

test("a load in progress names the cities, the share and the time left", () => {
  const view = progressView({
    active: true,
    percent: 42.6,
    eta_seconds: 25 * 60,
    cities_ready: 3,
    cities_total: 6,
    cities: [
      { id: "phl", name: "Philadelphia", phase: "done", ready: true },
      { id: "chi", name: "Chicago", phase: "fetch", ready: false },
      { id: "sea", name: "Seattle", phase: null, ready: false },
    ],
  });
  assert.equal(view.show, true);
  assert.equal(view.percent, 42.6);
  assert.equal(view.text, "Loading crime data · 3 of 6 cities ready · 42% · about 25 min left");
  assert.equal(view.detail, "Chicago: downloading incidents · Seattle: waiting to start");
});

test("before an estimate exists the bar says it is working one out", () => {
  const view = progressView({
    active: true, percent: 1, eta_seconds: null, cities_ready: 0, cities_total: 1,
    cities: [{ id: "phl", name: "Philadelphia", phase: "fetch", ready: false }],
  });
  assert.match(view.text, /0 of 1 city ready · 1% · estimating time left$/);
});

test("the bar never runs past either end", () => {
  assert.equal(progressView({ active: true, percent: 140, cities: [] }).percent, 100);
  assert.equal(progressView({ active: true, percent: -3, cities: [] }).percent, 0);
});
