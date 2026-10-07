// Unit tests for web/html.js, the escape-by-default HTML builder (F1).
// Run with `node --test tests/js/`; CI runs the same command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../../web/html.js", import.meta.url), "utf8");
const ctx = { URL };
vm.runInNewContext(src, ctx);
const { html, esc, safeHttpUrl } = ctx;

test("element content is escaped", () => {
  const out = html`<td>${"<img src=x onerror=alert(1)>"}</td>`.toString();
  assert.ok(out.includes("&lt;img"));
  assert.ok(!out.includes("<img"));
});

test("attribute context cannot be broken out of", () => {
  const out = html`<a title="${'" onmouseover="x'}">`.toString();
  const after = out.slice(out.indexOf('title="') + 'title="'.length);
  // The only unescaped quote left is the closing one the author wrote.
  assert.equal(after, '&quot; onmouseover=&quot;x">');
});

test("nested html fragments compose without double escaping", () => {
  const out = html`<ul>${["a<b", "c"].map((x) => html`<li>${x}</li>`)}</ul>`.toString();
  assert.equal(out, "<ul><li>a&lt;b</li><li>c</li></ul>");
});

test("null, undefined and false render empty; 0 renders", () => {
  assert.equal(html`[${null}${undefined}${false}]`.toString(), "[]");
  assert.equal(html`${0}`.toString(), "0");
});

test("esc escapes all five significant characters", () => {
  assert.equal(esc(`&<>"'`), "&amp;&lt;&gt;&quot;&#39;");
  assert.equal(esc(null), "");
});

test("safeHttpUrl only lets http(s) through", () => {
  assert.equal(safeHttpUrl("javascript:alert(1)"), null);
  assert.equal(safeHttpUrl("data:text/html,x"), null);
  assert.equal(safeHttpUrl("https://x.test/a b"), "https://x.test/a%20b");
  assert.equal(safeHttpUrl("not a url"), null);
  assert.equal(safeHttpUrl("http://x.test/"), "http://x.test/");
});
