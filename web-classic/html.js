/* Escape-by-default HTML building (F1). Every value interpolated into html`` is
   escaped unless it is itself the result of html`` (a SafeHtml). Arrays are
   joined after escaping each item, so .map(x => html`<li>${x}</li>`) composes.

   Why a tag rather than wrapping values by hand: with plain template literals
   every interpolation is raw, so each new field is one forgotten wrap away from
   injecting third-party text (offence descriptions, registry prose) as markup.
   Here the safe thing is the default and the literal parts -- which only the
   author writes -- are the only markup.

   A classic script, not a module: it attaches to globalThis so the browser (a
   plain <script> before app.js) and Node's vm (tests/js/html.test.mjs) can both
   load it unchanged. */
(function (root) {
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  class SafeHtml {
    constructor(s) {
      this.s = s;
    }
    toString() {
      return this.s;
    }
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
  }

  function part(v) {
    if (v instanceof SafeHtml) return v.s;
    if (Array.isArray(v)) return v.map(part).join("");
    // `false` renders as nothing, so ${cond && html`...`} works.
    if (v === null || v === undefined || v === false) return "";
    return esc(v);
  }

  function html(strings, ...values) {
    let out = strings[0];
    values.forEach((v, i) => {
      out += part(v) + strings[i + 1];
    });
    return new SafeHtml(out);
  }

  /** An http(s) URL, normalised, or null -- never javascript:/data:. */
  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value));
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch {
      return null;
    }
  }

  root.html = html;
  root.esc = esc;
  root.safeHttpUrl = safeHttpUrl;
  root.SafeHtml = SafeHtml;
})(globalThis);
