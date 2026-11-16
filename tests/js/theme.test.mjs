// Unit tests for web/theme.js: the light / dark choice and its storage.
// Run with `node --test tests/js/`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/theme.js", import.meta.url), "utf8");
const ctx = {};
vm.runInNewContext(src, ctx);
const { resolveTheme, savedTheme, saveTheme, THEME_KEY } = ctx;

const memory = () => {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
  };
};
const broken = {
  getItem() { throw new Error("SecurityError"); },
  setItem() { throw new Error("QuotaExceededError"); },
  removeItem() { throw new Error("SecurityError"); },
};

test("with no saved choice the system decides", () => {
  assert.equal(resolveTheme(null, true), "dark");
  assert.equal(resolveTheme(null, false), "light");
});

test("a saved choice wins over the system", () => {
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme("dark", false), "dark");
});

test("an unknown saved value is ignored", () => {
  assert.equal(resolveTheme("sepia", true), "dark");
  const store = memory();
  store.setItem(THEME_KEY, "sepia");
  assert.equal(savedTheme(store), null);
});

test("a choice round-trips through storage", () => {
  const store = memory();
  assert.equal(saveTheme(store, "dark"), true);
  assert.equal(savedTheme(store), "dark");
});

test("storage that throws never breaks the page", () => {
  assert.equal(savedTheme(broken), null);
  assert.equal(saveTheme(broken, "dark"), false);
  assert.equal(savedTheme(null), null);
});
