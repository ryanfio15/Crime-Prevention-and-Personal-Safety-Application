/* Directions: the pure half -- formatting, sentences and map features built
   from a POST /api/v1/directions response. No DOM and no fetch, so Node's vm
   (tests/js/directions.test.mjs) runs it unchanged; app.js does the rest.

   Every provider string (street names, instructions, line names, stop names)
   arrives already cleaned server-side (safety/routing/schema.py clean_text) and
   is still only ever rendered through html`` or textContent. Colours are the
   one provider value that reaches a style, so safeHex re-validates them here
   rather than trusting the server's check alone. */

/** "<1 min", "12 min", "1 h 5 min". */
function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return "<1 min";
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

/** "850 m", "1.2 km", "14 km" -- metric, like the map's scale bar. */
function formatDistance(metres) {
  if (!Number.isFinite(metres) || metres < 0) return "—";
  if (metres < 950) return `${Math.max(10, Math.round(metres / 10) * 10)} m`;
  const km = metres / 1000;
  return km < 10 ? `${km.toFixed(1)} km` : `${Math.round(km)} km`;
}

/** A ratio as the panel writes one (formatRatio in app.js): 85%, 1.4x past 10. */
function ratioText(ratio) {
  if (ratio >= 10) return `${ratio.toFixed(1)}×`;
  if (ratio < 0.005) return "<1%";
  return `${Math.round(ratio * 100)}%`;
}

const SENSE_WORDS = {
  higher: "higher than the city average",
  lower: "lower than the city average",
  about: "about the city average",
};

/**
 * One route's reported activity, in words, for one track.
 *
 * Always relative to the city and always about the route as a whole -- never
 * "safe", never a figure for either address (design doc S13).
 */
function activitySentence(activity, trackLabel) {
  if (!activity || !activity.comparable) {
    return {
      headline: "Not enough data to compare",
      detail:
        `Too little of this route passes through cells with a ${trackLabel} ` +
        `rating to compare it with the city.`,
    };
  }
  const words = SENSE_WORDS[activity.sense] ?? SENSE_WORDS.about;
  const parts = [`${ratioText(activity.mean_ratio)} of the city average along the route`];
  if (activity.peak_ratio !== null && activity.peak_ratio >= 2) {
    parts.push(`one stretch reaches ${ratioText(activity.peak_ratio)}`);
  }
  if (activity.rated_share < 0.95) {
    parts.push(`${Math.round(activity.rated_share * 100)}% of the route is rated`);
  }
  return {
    headline: `Reported ${trackLabel} activity ${words}`,
    detail: parts.join(" · "),
  };
}

/** Why there is no "lower reported activity" card, or null when there is one. */
function pickReason(pick, mode) {
  if (!pick || pick.lower !== null) return null;
  const allowance = formatDuration(pick.extra_allowed_s);
  switch (pick.why) {
    case "single_route":
      return mode === "transit"
        ? "Only one transit option was found for this trip."
        : "Only one distinct route was found for this trip.";
    case "not_rated":
      return "Too little of the fastest route is rated to compare routes.";
    case "too_slow":
      return `A route with lower reported activity exists, but it takes more than ${allowance} longer.`;
    case "not_lower":
      return `No other route within ${allowance} of the fastest has clearly lower reported activity.`;
    default:
      return null;
  }
}

/**
 * The route with the lowest average reported activity on `track` (its
 * mean_ratio, the figure its card is coloured by), among the routes rated well
 * enough to compare; a tie goes to the quicker. Null with fewer than two such
 * routes: with nothing to compare against, no route is the safest.
 */
function safestIndex(result, track) {
  const rated = result.routes
    .map((route, index) => ({ index, route, activity: route.activity?.[track] }))
    .filter(({ activity }) => activity?.comparable && typeof activity.mean_ratio === "number");
  if (rated.length < 2) return null;
  rated.sort((a, b) => a.activity.mean_ratio - b.activity.mean_ratio || a.route.duration_s - b.route.duration_s);
  return rated[0].index;
}

/**
 * The cards to show, in order: the safest first, then the fastest, then any
 * other alternatives -- or the fastest first when no route can be called the
 * safest. `role` is what the card is labelled with.
 */
function routeCardModel(result, track) {
  const pick = result.picks?.[track] ?? { fastest: 0 };
  const safest = safestIndex(result, track);
  const order = safest === null ? [pick.fastest] : [safest];
  if (!order.includes(pick.fastest)) order.push(pick.fastest);
  result.routes.forEach((_, i) => {
    if (!order.includes(i)) order.push(i);
  });
  const fastest = result.routes[pick.fastest];
  return order.map((index) => {
    const route = result.routes[index];
    const extra = route.duration_s - fastest.duration_s;
    let role = "Alternative";
    if (index === safest && index === pick.fastest) role = "Fastest and safest";
    else if (index === safest) role = "Safest";
    else if (index === pick.fastest) role = "Fastest";
    return {
      index,
      role,
      duration: formatDuration(route.duration_s),
      distance: formatDistance(route.distance_m),
      extra: index === pick.fastest || extra < 30 ? null : `+${formatDuration(extra)}`,
      activity: route.activity?.[track] ?? null,
    };
  });
}

const HEX = /^#[0-9a-f]{6}$/i;

/** A '#rrggbb' colour, or null. Accepts the bare six digits GTFS uses. */
function safeHex(value) {
  if (typeof value !== "string") return null;
  const hex = value.startsWith("#") ? value : `#${value}`;
  return HEX.test(hex) ? hex.toLowerCase() : null;
}

/** Black or white text on a background, by WCAG relative luminance. */
function textOn(hex) {
  const bg = safeHex(hex);
  if (!bg) return "#ffffff";
  const channel = (i) => {
    const c = parseInt(bg.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const l = 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  // The luminance where black and white text have equal contrast.
  return l > 0.179 ? "#0b0b0b" : "#ffffff";
}

const LEG_VERBS = { WALK: "Walk", BIKE: "Bike", CAR: "Drive", RENTAL: "Ride" };

/** One transit leg as a short line: "Walk 4 min" or "Red toward Shady Grove". */
function transitLegSummary(leg) {
  if (!leg.transit) {
    const verb = LEG_VERBS[leg.mode] ?? "Walk";
    const far = Number.isFinite(leg.distance_m) ? ` · ${formatDistance(leg.distance_m)}` : "";
    return { kind: "street", text: `${verb} ${formatDuration(leg.duration_s)}${far}` };
  }
  const bg = safeHex(leg.color);
  const fg = safeHex(leg.text_color) ?? (bg ? textOn(bg) : null);
  const stops = Number.isInteger(leg.num_stops) && leg.num_stops > 0
    ? ` · ${leg.num_stops} stop${leg.num_stops === 1 ? "" : "s"}`
    : "";
  return {
    kind: "ride",
    name: leg.route_name || leg.mode.toLowerCase(),
    color: bg,
    textColor: fg,
    text: `${leg.headsign ? `toward ${leg.headsign}` : ""}`.trim(),
    detail: `${formatDuration(leg.duration_s)}${stops}`,
  };
}

/** A clock time in the city's own zone: "5:42 PM". */
function clockIn(iso, timeZone) {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  try {
    return at.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone });
  } catch {
    return at.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }
}

/** Run `fn` once `ms` have passed without another call. */
function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

/** "39.9526, -75.1652" (or with a space, or in brackets) as a point, else null.
    For the addresses Photon cannot find: a pasted coordinate always works. */
function latLngFromString(text) {
  const m = String(text ?? "")
    .trim()
    .match(/^[([]?\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*[)\]]?$/);
  if (!m) return null;
  const lat = Number(m[1]);
  const lng = Number(m[2]);
  if (!(Math.abs(lat) <= 90 && Math.abs(lng) <= 180)) return null;
  return { lat, lng };
}

/** [[west, south], [east, north]] around every route's line. */
function routesBbox(routes) {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  for (const route of routes) {
    for (const [lng, lat] of route.geometry?.coordinates ?? []) {
      if (lng < w) w = lng;
      if (lng > e) e = lng;
      if (lat < s) s = lat;
      if (lat > n) n = lat;
    }
  }
  return Number.isFinite(w) ? [[w, s], [e, n]] : null;
}

/** The selected route's activity segments as map features; `ratio` -1 marks
    an unrated stretch, drawn in the neutral colour like an unrated cell. */
function segmentFeatures(activity) {
  return {
    type: "FeatureCollection",
    features: (activity?.segments ?? []).map((seg) => ({
      type: "Feature",
      properties: { ratio: seg.ratio === null ? -1 : seg.ratio },
      geometry: { type: "LineString", coordinates: seg.coordinates },
    })),
  };
}

/** The selected route's own line: one feature per transit leg (coloured by
    the line, its walks marked to draw dotted), or one for a street route. */
function routeLineFeatures(route) {
  if (route.mode !== "transit" || !route.legs?.length) {
    return {
      type: "FeatureCollection",
      features: [{ type: "Feature", properties: { walk: false, color: null }, geometry: route.geometry }],
    };
  }
  return {
    type: "FeatureCollection",
    features: route.legs.map((leg) => ({
      type: "Feature",
      properties: { walk: !leg.transit, color: leg.transit ? safeHex(leg.color) : null },
      geometry: leg.geometry,
    })),
  };
}

/** Every route but the selected one, for the grey alternatives layer. */
function altFeatures(routes, selected) {
  return {
    type: "FeatureCollection",
    features: routes
      .map((route, index) => ({ type: "Feature", properties: { index }, geometry: route.geometry }))
      .filter((f) => f.properties.index !== selected),
  };
}

/** The key a result is kept under in the page, so switching back to a mode
    already asked for shows it again without asking the server. */
function directionsKey(req) {
  const pt = (p) => `${p.lat.toFixed(5)},${p.lng.toFixed(5)}`;
  return [pt(req.from), pt(req.to), req.mode, req.depart_at ?? "now", req.window ?? ""].join("|");
}

/* ---------------------------------------------------------------- navigation

   Turn-by-turn on the phone: the chosen route as a list of steps, and where
   the visitor is along it. Everything here is geometry on [lng, lat] pairs;
   app.js feeds it GPS fixes and renders what comes back. */

/* Metres per degree of latitude; a degree of longitude is this times cos(lat). */
const M_PER_DEG = 111_320;

/** The steps to follow, in order. A street route has one per maneuver, each
    with the stretch of the route it covers; a transit route one per leg --
    walk to the stop, ride to the stop to get off at -- each with its leg's
    line and the time it ends. `points` are [lng, lat] pairs. */
function navSteps(route, timeZone) {
  const coords = route.geometry?.coordinates ?? [];
  if (route.mode === "transit" && route.legs?.length) {
    return route.legs.map((leg, i) => {
      const s = transitLegSummary(leg);
      const last = i === route.legs.length - 1;
      const to = leg.destination?.name;
      const ends = leg.end_time ? Date.parse(leg.end_time) : NaN;
      const points = leg.geometry?.coordinates ?? [];
      if (s.kind === "street") {
        return {
          kind: "street",
          text: last || !to ? "Walk to your destination" : `Walk to ${to}`,
          sub: s.text,
          points,
          endsAt: Number.isFinite(ends) ? ends : null,
        };
      }
      const leaves = leg.start_time ? clockIn(leg.start_time, timeZone) : "";
      return {
        kind: "ride",
        text: `Take ${s.name}${s.text ? ` ${s.text}` : ""}`,
        sub: [
          leg.origin?.name ? `Board at ${leg.origin.name}${leaves ? ` · ${leaves}` : ""}` : leaves,
          to ? `get off at ${to}` : "",
          s.detail,
        ].filter(Boolean).join(" · "),
        chip: { name: s.name, color: s.color, textColor: s.textColor },
        points,
        endsAt: Number.isFinite(ends) ? ends : null,
      };
    });
  }
  return (route.steps ?? []).map((step) => {
    const end = Math.max(step.end_index, step.begin_index);
    return {
      kind: "street",
      text: step.instruction,
      sub: step.distance_m > 0 ? formatDistance(step.distance_m) : "",
      street: step.street_names?.[0] ?? "",
      points: coords.slice(step.begin_index, end + 1),
      endsAt: null,
    };
  });
}

/** Where `p` ({lat, lng}) falls on the line `points`: its distance from the
    line, how far along the line its nearest point is, and the line's length,
    all in metres. A flat projection around `p` -- exact enough over a city
    block, which is all one step spans. Null for an empty line. */
function projectOnLine(points, p) {
  if (!points?.length) return null;
  const kx = M_PER_DEG * Math.cos((p.lat * Math.PI) / 180);
  const xy = ([lng, lat]) => [(lng - p.lng) * kx, (lat - p.lat) * M_PER_DEG];
  let best = { dist: Infinity, along: 0 };
  let walked = 0;
  let [ax, ay] = xy(points[0]);
  if (points.length === 1) {
    return { dist: Math.hypot(ax, ay), along: 0, total: 0 };
  }
  for (let i = 1; i < points.length; i++) {
    const [bx, by] = xy(points[i]);
    const dx = bx - ax;
    const dy = by - ay;
    const len = Math.hypot(dx, dy);
    // The visitor is the origin; t is how far along this segment the foot
    // of the perpendicular falls, clamped to the segment.
    const t = len ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / (len * len))) : 0;
    const dist = Math.hypot(ax + t * dx, ay + t * dy);
    if (dist < best.dist) best = { dist, along: walked + t * len };
    walked += len;
    [ax, ay] = [bx, by];
  }
  return { ...best, total: walked };
}

/* How close to a step's end counts as having reached it: inside a GPS fix's
   usual error on a phone, so a turn is passed when the visitor is at it. */
const NAV_STEP_DONE_M = 15;
/* How far ahead to look for the step the visitor is on. Small, so a route
   that doubles back past itself is not skipped ahead on. */
const NAV_LOOKAHEAD = 3;

/**
 * Which step the visitor is on, from a fix `p` ({lat, lng}) and the step
 * they were on (`from`). Never goes back: a step once passed stays passed.
 *
 * A step is passed when the visitor is on a later one, within
 * NAV_STEP_DONE_M of its end, or -- for a transit leg, where a fix
 * underground may be missing or wrong -- more than a minute past the time
 * it ends. Returns the step index, the distance off the route, and the
 * distance left in the step; index === steps.length means arrived.
 */
function navProgress(steps, p, from = 0, now = Date.now()) {
  let index = Math.max(0, from);
  // Timetable first: a leg that has ended is behind the visitor whatever the
  // fix says.
  while (index < steps.length - 1 && steps[index].endsAt && steps[index].endsAt + 60_000 < now) {
    index += 1;
  }
  let best = null;
  const last = Math.min(steps.length - 1, index + NAV_LOOKAHEAD);
  for (let i = index; i <= last; i++) {
    const at = projectOnLine(steps[i].points, p);
    if (!at) continue;
    // Strictly nearer, so the earlier of two equally near steps wins: where
    // one ends the next begins, and the visitor is still at the turn.
    if (!best || at.dist < best.at.dist - 1) best = { i, at };
  }
  if (!best) return { index, offRoute: null, remaining: null };
  index = best.i;
  let remaining = Math.max(0, best.at.total - best.at.along);
  // At a step's end the next is the one to show -- and so on through a
  // street route's zero-length "you have arrived", to arrival itself.
  while (remaining < NAV_STEP_DONE_M) {
    index += 1;
    if (index >= steps.length) return { index: steps.length, offRoute: best.at.dist, remaining: 0 };
    const next = projectOnLine(steps[index].points, p);
    if (!next) break;
    remaining = Math.max(0, next.total - next.along);
  }
  return { index, offRoute: best.at.dist, remaining };
}

/** Compass heading 0-360 (clockwise from north) from a deviceorientation
    event, or null when it carries none. iOS gives webkitCompassHeading; the
    absolute event elsewhere gives alpha, counter-clockwise from north, which
    is turned round and corrected for the screen's own rotation. */
function headingFromOrientation(event, screenAngle = 0) {
  if (typeof event.webkitCompassHeading === "number" && Number.isFinite(event.webkitCompassHeading)) {
    return ((event.webkitCompassHeading % 360) + 360) % 360;
  }
  if (event.absolute && typeof event.alpha === "number" && Number.isFinite(event.alpha)) {
    return (((360 - event.alpha + screenAngle) % 360) + 360) % 360;
  }
  return null;
}
