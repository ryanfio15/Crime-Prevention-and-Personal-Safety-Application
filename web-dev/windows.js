/* Time windows are per city (gold.city_window, migration 018): every city has
   3, 6 and 9 months, then cumulative years back to its own oldest stored
   incident. The list arrives with the city record (/api/v1/cities/{id}); these
   helpers decide what the window control offers and what it says.

   The map is driven by two calendar dates (migration 020). The stored windows
   stay as one-click presets that fill them in, and a range that equals one is
   requested as that window, so it keeps everything the stored build carries.
   Any other range is ranked by the server on request. Dates are ISO strings
   ("2025-10-09") throughout and the arithmetic is done in UTC, so a daylight-
   saving change can never move a day.

   A classic script, not a module, for the same reason as html.js: the browser
   loads it with a plain <script> before app.js, and Node's vm loads it unchanged
   in tests/js/windows.test.mjs. */
(function (root) {
  /* The previous release's names and the windows they now are. Mirrors
     safety.api.repository.LEGACY_WINDOWS. */
  const LEGACY_WINDOWS = { last_90d: "last_3m", last_12m: "last_1y", last_24m: "last_2y" };
  /* Forward only: a saved link to the retired 30-day window opens on 3 months. */
  const ALIASES = { ...LEGACY_WINDOWS, last_30d: "last_3m" };
  const DEFAULT_WINDOW = "last_1y";

  /** The served window an id means, accepting the legacy names both ways. */
  function findWindow(windows, id) {
    if (!id || !windows) return null;
    const byId = new Map(windows.map((w) => [w.id, w]));
    if (byId.has(id)) return byId.get(id);
    if (byId.has(ALIASES[id])) return byId.get(ALIASES[id]);
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

  /* ------------------------------------------------------------ date ranges */

  const parseIso = (iso) => {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  };
  const formatIso = (date) => date.toISOString().slice(0, 10);

  /** A Date's own local calendar day, as an ISO string. */
  function localIso(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function addDays(iso, days) {
    const date = parseIso(iso);
    date.setUTCDate(date.getUTCDate() + days);
    return formatIso(date);
  }

  /** The same day `years` earlier; 29 February becomes the 28th, as in
      safety.etl.gold._shift_years. */
  function yearsBefore(iso, years) {
    const [y, m, d] = iso.split("-").map(Number);
    const target = new Date(Date.UTC(y - years, m - 1, d));
    if (target.getUTCMonth() !== m - 1) target.setUTCDate(0);
    return formatIso(target);
  }

  /** Inclusive day count. */
  const spanDays = (range) => Math.round((parseIso(range.to) - parseIso(range.from)) / 86400000) + 1;

  /** The opening range: the year up to and including today, the way the stored
      last_1y spans the year up to the newest reported day. */
  function defaultRange(today) {
    return { from: addDays(yearsBefore(today, 1), 1), to: today };
  }

  /** Put the dates in order and inside [min, max]. ISO strings compare as dates. */
  function clampRange(range, min, max) {
    let { from, to } = range;
    if (from > to) [from, to] = [to, from];
    const clamp = (iso) => (min && iso < min ? min : max && iso > max ? max : iso);
    return { from: clamp(from), to: clamp(to) };
  }

  /** Where the server stops the range: no later than the newest reported day,
      and never before its start. Mirrors safety.api.main._resolve_range. */
  function effectiveEnd(range, dataEnd) {
    const end = dataEnd && range.to > dataEnd ? dataEnd : range.to;
    return end < range.from ? range.from : end;
  }

  /** The stored window these dates are exactly, or null for a custom range. */
  function presetFor(range, windows, dataEnd) {
    if (!range || !windows) return null;
    const end = effectiveEnd(range, dataEnd);
    return windows.find((w) => w.start === range.from && w.end === end) || null;
  }

  /** The dates a preset fills in: its own start, and today where today is past
      the window's end (the server clamps it back, so it still matches). */
  function presetRange(win, today) {
    return { from: win.start, to: today && today > win.end ? today : win.end };
  }

  const dayMonthYear = (iso) =>
    parseIso(iso).toLocaleDateString("en-US", {
      day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
    });

  /** Below this many days a range holds too little per cell for the ranking to
      separate much; the shortest stored window is three months. */
  const SHORT_RANGE_DAYS = 90;

  /**
   * The note under the date controls, as plain text. A preset says what its
   * stored window says (windowNote); a custom range says the same kinds of
   * thing, worked out from the dates. Both say where the data stops when the
   * range runs past it -- the default range ends today, and every city
   * publishes with a lag.
   */
  function rangeNote(range, city, res) {
    if (!range || !city) return "";
    const windows = city.windows || [];
    const dataEnd = city.coverage_end || null;
    const parts = [];
    if (dataEnd && range.to > dataEnd) parts.push(`Data reported through ${dayMonthYear(dataEnd)}.`);

    const preset = presetFor(range, windows, dataEnd);
    if (preset) {
      const note = windowNote(preset, res);
      if (note) parts.push(note);
      return parts.join(" ");
    }
    if (!(city.custom_range_resolutions || []).includes(res)) {
      parts.push("Custom dates are not served at this cell size — pick a preset range.");
      return parts.join(" ");
    }
    const end = effectiveEnd(range, dataEnd);
    if (spanDays({ from: range.from, to: end }) < SHORT_RANGE_DAYS) {
      parts.push("A short range holds few incidents per cell, so many cells tie in the ranking.");
    }
    for (const c of city.series_caveats || []) {
      // period_to is exclusive.
      if (c.from <= end && c.to > range.from) parts.push(c.text);
    }
    return parts.join(" ");
  }

  root.LEGACY_WINDOWS = LEGACY_WINDOWS;
  root.DEFAULT_WINDOW = DEFAULT_WINDOW;
  root.findWindow = findWindow;
  root.pickWindow = pickWindow;
  root.windowBuiltAt = windowBuiltAt;
  root.windowNote = windowNote;
  root.localIso = localIso;
  root.addDays = addDays;
  root.yearsBefore = yearsBefore;
  root.spanDays = spanDays;
  root.defaultRange = defaultRange;
  root.clampRange = clampRange;
  root.effectiveEnd = effectiveEnd;
  root.presetFor = presetFor;
  root.presetRange = presetRange;
  root.rangeNote = rangeNote;
})(globalThis);
