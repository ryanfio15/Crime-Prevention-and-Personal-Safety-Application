/* Time of day is always "now": the city's current local time, and whether the
   current hour is historically busier or quieter than the city's average hour.

   Not live data and not a prediction. The hour blocks are the city's own local
   clock (reference.source_registry.timezone), so "now" is read in that zone
   rather than the browser's -- a visitor in Chicago looking at Philadelphia
   sees Philadelphia's time. The comparison reads the stored hourly profile:
   how this hour of the day has usually compared with the rest of the day.

   A classic script, not a module, for the same reason as html.js and
   windows.js: the browser loads it with a plain <script> before app.js, and
   Node's vm loads it unchanged in tests/js/hours.test.mjs. */
(function (root) {
  const HOUR_BLOCKS = 24;
  /* Inside ±10% the honest reading is "about average": hour-to-hour counts
     move by that much on noise alone. */
  const USUAL_DEADBAND = 0.1;
  /* Below this many incidents across the whole day one hour against a
     twenty-fourth of the total is a ratio of single digits. */
  const MIN_HOUR_EVIDENCE = 48;

  /** The local hour 0-23 at `date` in IANA zone `timeZone`; the browser's own
      hour when the zone is missing or unknown to this browser. */
  function cityHour(timeZone, date = new Date()) {
    if (timeZone) {
      try {
        const text = new Intl.DateTimeFormat("en-US", {
          timeZone,
          hour: "numeric",
          hourCycle: "h23",
        }).format(date);
        const hour = Number.parseInt(text, 10);
        if (Number.isInteger(hour) && hour >= 0 && hour < HOUR_BLOCKS) return hour;
      } catch {
        /* RangeError for a zone this browser does not know; fall through. */
      }
    }
    return date.getHours();
  }

  /** The local time at `date` in `timeZone` for display, e.g. "10:13 PM"; the
      browser's own clock when the zone is unknown. */
  function cityTimeLabel(timeZone, date = new Date()) {
    const options = { hour: "numeric", minute: "2-digit" };
    if (timeZone) {
      try {
        return new Intl.DateTimeFormat("en-US", { ...options, timeZone }).format(date);
      } catch {
        /* RangeError for a zone this browser does not know; fall through. */
      }
    }
    return new Intl.DateTimeFormat("en-US", options).format(date);
  }

  /**
   * This hour against the city's average hour, from the 24-block profile.
   *
   * Returns null when the day holds too little to compare; otherwise the
   * direction ("up", "down" or "flat"), the change as a fraction of the
   * average hour, and the counts behind it.
   */
  function hourVsUsual(counts, hour, { minTotal = MIN_HOUR_EVIDENCE } = {}) {
    if (!Array.isArray(counts) || counts.length !== HOUR_BLOCKS) return null;
    if (!Number.isInteger(hour) || hour < 0 || hour >= HOUR_BLOCKS) return null;
    const total = counts.reduce((sum, n) => sum + n, 0);
    if (total < minTotal) return null;

    const average = total / HOUR_BLOCKS;
    const change = counts[hour] / average - 1;
    const direction =
      change > USUAL_DEADBAND ? "up" : change < -USUAL_DEADBAND ? "down" : "flat";
    return { direction, change, count: counts[hour], average, max: Math.max(...counts, 1) };
  }

  root.cityHour = cityHour;
  root.cityTimeLabel = cityTimeLabel;
  root.hourVsUsual = hourVsUsual;
})(globalThis);
