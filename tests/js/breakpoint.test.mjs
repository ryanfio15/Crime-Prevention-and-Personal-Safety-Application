// One breakpoint, 768 px, shared by the stylesheet and the script (UI 5).
// A second width in either is the 600 / 900 px mix this replaced coming back.
// Run with `node --test tests/js/`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const PHONE = "(max-width: 767.98px)";
const css = readFileSync(new URL("../../web/styles.css", import.meta.url), "utf8");
const app = readFileSync(new URL("../../web/app.js", import.meta.url), "utf8");

test("every width media query in styles.css is the one breakpoint", () => {
  const widths = [...css.matchAll(/\((?:min|max)-width:[^)]*\)/g)].map((m) => m[0]);
  assert.ok(widths.length > 0, "styles.css has no phone layout");
  for (const w of widths) assert.equal(w, PHONE);
});

test("app.js reads the same breakpoint", () => {
  const declared = app.match(/const NARROW_QUERY = "([^"]+)";/);
  assert.ok(declared, "NARROW_QUERY not found in web/app.js");
  assert.equal(declared[1], PHONE);
  // Nothing else asks matchMedia about a width.
  const others = [...app.matchAll(/\((?:min|max)-width:[^)]*\)/g)].map((m) => m[0]);
  assert.deepEqual([...new Set(others)], [PHONE]);
});
