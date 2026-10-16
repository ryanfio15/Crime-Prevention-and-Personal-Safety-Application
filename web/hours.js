/* Time of day is always "now": the city's current local time, whether the
   current hour is historically busier or quieter than the city's average hour,
   which way the next few hours usually go, and the comparatively quietest
   stretch coming up.

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

  /* How far ahead the outlook looks, and how far ahead (and how wide) the
     lower-risk window search runs. Three-hour stretches: one block is too
     noisy to point anyone at, and a stretch is something a person can plan
     around. */
  const OUTLOOK_SPAN = 3;
  const WINDOW_HORIZON = 12;
  const WINDOW_WIDTH = 3;

  /** Mean of `width` blocks from `start`, wrapping past midnight. */
  function stretchMean(counts, start, width) {
    let sum = 0;
    for (let i = 0; i < width; i++) sum += counts[(start + i) % HOUR_BLOCKS];
    return sum / width;
  }

  /**
   * Whether the next `span` blocks are historically busier or quieter than
   * the current one: the mean of the blocks ahead against this block.
   *
   * Returns null when the day holds too little to compare; otherwise the
   * direction ("up", "down" or "flat") and the change as a fraction of the
   * current block (null when the current block is empty, where a percentage
   * would be infinite).
   */
  function hourOutlook(counts, hour, { span = OUTLOOK_SPAN, minTotal = MIN_HOUR_EVIDENCE } = {}) {
    if (!hourVsUsual(counts, hour, { minTotal })) return null;
    const now = counts[hour];
    const ahead = stretchMean(counts, (hour + 1) % HOUR_BLOCKS, span);
    if (now === 0) return { direction: ahead > 0 ? "up" : "flat", change: null, span };
    const change = ahead / now - 1;
    const direction =
      change > USUAL_DEADBAND ? "up" : change < -USUAL_DEADBAND ? "down" : "flat";
    return { direction, change, span };
  }

  /**
   * The comparatively lowest-risk stretch coming up: of every `width`-block
   * stretch starting within the next `horizon` hours (the current block
   * included), the one with the fewest incidents historically.
   *
   * Returns null when the day holds too little to compare; otherwise its
   * first block, its end block (exclusive, wrapped), whether it starts now,
   * and how it compares with the average hour.
   */
  function quietestWindow(
    counts,
    hour,
    { horizon = WINDOW_HORIZON, width = WINDOW_WIDTH, minTotal = MIN_HOUR_EVIDENCE } = {}
  ) {
    const usual = hourVsUsual(counts, hour, { minTotal });
    if (!usual) return null;
    let best = null;
    for (let offset = 0; offset + width <= horizon; offset++) {
      const start = (hour + offset) % HOUR_BLOCKS;
      const mean = stretchMean(counts, start, width);
      // Strictly lower only: on a tie the earlier stretch wins, which is the
      // more useful answer to "when, soonest".
      if (!best || mean < best.mean) best = { start, offset, mean };
    }
    return {
      start: best.start,
      end: (best.start + width) % HOUR_BLOCKS,
      startsNow: best.offset === 0,
      hoursAway: best.offset,
      vsAverage: best.mean / usual.average - 1,
      blocks: Array.from({ length: width }, (_, i) => (best.start + i) % HOUR_BLOCKS),
    };
  }

  /** "3 AM", "12 PM", "12 AM" -- an hour block's start, as the clock reads it. */
  function hourName(hour) {
    const h = ((hour % HOUR_BLOCKS) + HOUR_BLOCKS) % HOUR_BLOCKS;
    return `${h % 12 === 0 ? 12 : h % 12} ${h < 12 ? "AM" : "PM"}`;
  }

  root.cityHour = cityHour;
  root.cityTimeLabel = cityTimeLabel;
  root.hourVsUsual = hourVsUsual;
  root.hourOutlook = hourOutlook;
  root.quietestWindow = quietestWindow;
  root.hourName = hourName;
})(globalThis);
