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
 * The cards to show, in order: the fastest first, then the lower-activity pick
 * if there is one, then any other alternatives. `role` is what the card is
 * labelled with.
 */
function routeCardModel(result, track) {
  const pick = result.picks?.[track] ?? { fastest: 0, lower: null };
  const order = [pick.fastest];
  if (pick.lower !== null && pick.lower !== pick.fastest) order.push(pick.lower);
  result.routes.forEach((_, i) => {
    if (!order.includes(i)) order.push(i);
  });
  const fastest = result.routes[pick.fastest];
  return order.map((index) => {
    const route = result.routes[index];
    const extra = route.duration_s - fastest.duration_s;
    let role = "Alternative";
    if (index === pick.fastest) role = "Fastest";
    else if (index === pick.lower) role = "Lower reported activity";
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
