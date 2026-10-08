/* Time windows are per city (gold.city_window, migration 018): every city has
   30 days and 3/6/9 months, then cumulative years back to its own oldest stored
   incident. The list arrives with the city record (/api/v1/cities/{id}); these
   helpers decide what the window control offers and what it says.

   A classic script, not a module, for the same reason as html.js: the browser
   loads it with a plain <script> before app.js, and Node's vm loads it unchanged
   in tests/js/windows.test.mjs. */
(function (root) {
  /* The previous release's names and the windows they now are. Mirrors
     safety.api.repository.LEGACY_WINDOWS. */
  const LEGACY_WINDOWS = { last_90d: "last_3m", last_12m: "last_1y", last_24m: "last_2y" };
  const DEFAULT_WINDOW = "last_1y";

  /** The served window an id means, accepting the legacy names both ways. */
  function findWindow(windows, id) {
    if (!id || !windows) return null;
    const byId = new Map(windows.map((w) => [w.id, w]));
    if (byId.has(id)) return byId.get(id);
    if (byId.has(LEGACY_WINDOWS[id])) return byId.get(LEGACY_WINDOWS[id]);
    const backward = Object.keys(LEGACY_WINDOWS).find((k) => LEGACY_WINDOWS[k] === id);
    return backward && byId.has(backward) ? byId.get(backward) : null;
  }

  /**
   * Which window to show after the list changes (a city switch, or a refresh
   * that grew the history).
   *
   * Keeps the current one if this city has it. Otherwise the longest window no
   * longer than the current one, so switching from Chicago's "last 20 years" to
   * Austin lands on Austin's longest rather than jumping to a year; then the
   * city's default; then the first.
   */
  function pickWindow(windows, currentId, currentSpanDays, defaultId) {
    if (!windows || !windows.length) return null;
    const kept = findWindow(windows, currentId);
    if (kept) return kept;
    if (Number.isFinite(currentSpanDays)) {
      const shorter = windows.filter((w) => w.span_days <= currentSpanDays);
      if (shorter.length) {
        return shorter.reduce((a, b) => (b.span_days > a.span_days ? b : a));
      }
    }
    return (
      findWindow(windows, defaultId) || findWindow(windows, DEFAULT_WINDOW) || windows[0]
    );
  }

  /** Whether the activity layer exists for this window at this cell size. */
  function windowBuiltAt(win, res) {
    return res !== 10 || Boolean(win.res10);
  }

  const monthYear = (iso) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString("en-US", { month: "short", year: "numeric" });

  /**
   * The note under the window control, as plain text: what is missing for this
   * window and why, and the series caveats for the period it reaches into.
   * Empty when there is nothing to say.
   */
  function windowNote(win, res) {
    if (!win) return "";
    const parts = [];
    if (res === 10 && !win.res10) {
      parts.push("Not built at this cell size — too few incidents per cell to rank.");
    }
    if (win.partial) parts.push(`Data from ${monthYear(win.data_start)}.`);
    if (!win.safety) {
      parts.push("Counts only: the safety ranking is not built for windows this long.");
    }
    for (const caveat of win.caveats || []) parts.push(caveat);
    return parts.join(" ");
  }

  root.LEGACY_WINDOWS = LEGACY_WINDOWS;
  root.DEFAULT_WINDOW = DEFAULT_WINDOW;
  root.findWindow = findWindow;
  root.pickWindow = pickWindow;
  root.windowBuiltAt = windowBuiltAt;
  root.windowNote = windowNote;
})(globalThis);
