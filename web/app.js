/* ---------------------------------------------------------------------------
   The map: every covered city at once, over the last year, coloured by each
   cell's relative risk against the rest of its own city at the hour and day it
   is now there. The only controls are Show tiles and the violent / non-violent
   toggle; there is no other data to choose.

   Reads only the precomputed gold-layer endpoints -- it never asks the server
   to aggregate anything (design doc S9.3). The one computation it does locally
   is H3 cell membership from GPS coordinates, which S10 points out is a pure
   function of (lat, lng, resolution) and needs no server round trip at all.
   --------------------------------------------------------------------------- */

const API = "/api/v1";

/* The city the map opens framed on, when the visitor's own location is not
   known. Every city is loaded either way; this only picks where to look first. */
const DEFAULT_CITY = "phl";

/** A colour token from styles.css, as the current theme sets it. MapLibre
    paints the map itself, so anything it draws in a page colour reads the
    token here rather than repeating the hex. */
function themeColor(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/* A distinct state, not the bottom of the ramp: nothing was reported here.
   Light keeps the original #e1e0d9; dark has its own (--zero-fill). */
const zeroFill = () => themeColor("--zero-fill", "#e1e0d9");
const primaryColor = () => themeColor("--primary", "#f06a3f");

/* UI 5's five map colours, low -> high (docs/ui5/UI5_SPEC.md), the same in
   light and dark. They sit on every fifth step of the 21-step ramp, so on
   RAMP_HINGES below: green at 0, light green at 0.75x, yellow exactly at the
   1.5x hinge, orange at 2x, red from 3x. The steps between are blended. */
const RAMP_ANCHORS = ["#3fae86", "#a8d47e", "#ffd166", "#ff9a6b", "#e85a72"];

/** `count` colours spread evenly over `anchors`, blended in sRGB. */
function spreadRamp(anchors, count) {
  const parse = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const span = (count - 1) / (anchors.length - 1);
  return Array.from({ length: count }, (_, i) => {
    const k = Math.min(Math.floor(i / span), anchors.length - 2);
    const t = i / span - k;
    const [a, b] = [parse(anchors[k]), parse(anchors[k + 1])];
    return `#${a.map((x, c) => Math.round(x + (b[c] - x) * t).toString(16).padStart(2, "0")).join("")}`;
  });
}

/* Twenty-one steps, the same count RAMP_HINGES is laid out over (0..20). */
const VALUE_RAMP = spreadRamp(RAMP_ANCHORS, 21);

/* Where the ramp is pinned, as [step, multiple of the city average]: green up
   to one and a half times the average, yellow there, orange by double, the
   deepest red from triple on. Yellow sits above the average rather than at it
   so an ordinary cell reads green -- a little over average is not a warning.
   Steps between two hinges are spread evenly across them. */
const RAMP_HINGES = [[0, 0], [10, 1.5], [15, 2], [20, 3]];

/**
 * Ramp stops laid out over a cell's multiple of its city average (1 = average).
 *
 * Colour says how a cell compares with the city rather than where it ranks:
 * up to 150% of the average is green, 150% is yellow, double is orange
 * and triple or more is red. Fixed multiples rather than the layer's own
 * min / median / max, so a colour means the same thing in every city and at
 * every hour, and a quiet city is allowed to come out mostly green.
 *
 * Above triple, MapLibre's interpolate holds the last stop, so outliers share
 * the deepest red instead of stretching the scale and washing everything else
 * out to orange.
 */
function valueRampStops(colors) {
  const stops = [];
  colors.forEach((hex, i) => {
    const k = RAMP_HINGES.findIndex(([step]) => step >= i);
    const [i1, r1] = RAMP_HINGES[k];
    const [i0, r0] = RAMP_HINGES[Math.max(k - 1, 0)];
    stops.push(i1 === i0 ? r1 : r0 + ((i - i0) / (i1 - i0)) * (r1 - r0), hex);
  });
  return stops;
}

/**
 * One city's average of `prop` across the cells of it that take a colour.
 *
 * Computed here rather than served, for the same reason the hourly count rank
 * is: the features are already in the browser, and the alternative is another
 * precomputed table existing only to colour one view. Cells that render in the
 * neutral fill are excluded -- they never take a colour from the ramp, so
 * letting them drag the average down would turn the rest of the city orange.
 *
 * A rate per person is averaged per person, weighted by each cell's exposure,
 * so the average is the city's rate and not the mean of its cells' rates -- the
 * same sense the ranking itself has. A count has no
 * denominator to weight by, and neither does a layer ranked by area, so those
 * take a plain mean.
 */
function valueDomain(features, prop, isPainted, perPerson) {
  let sum = 0;
  let painted = 0;
  let weighted = 0;
  let people = 0;
  for (const feature of features) {
    const p = feature.properties;
    if (!isPainted(p)) continue;
    const value = p[prop];
    if (typeof value !== "number") continue;
    sum += value;
    painted += 1;
    if (perPerson && typeof p.exposure === "number" && p.exposure > 0) {
      weighted += value * p.exposure;
      people += p.exposure;
    }
  }
  if (!painted) return null;
  const average = people > 0 ? weighted / people : sum / painted;
  return average > 0 ? { average, painted } : null;
}

/** A cell's multiple of its city average as the page writes it: "0.78×"
    (ratioText, web/directions.js, which also names the bottom end rather
    than rounding it to zero). */
const formatRatio = (ratio) => ratioText(ratio);

/* The legend's bands, from the hinges above zero (1.5, 2, 3): a band always
   matches the colour the map paints (bandOf, web/directions.js). */
const BAND_EDGES = RAMP_HINGES.slice(1).map(([, ratio]) => ratio);
const band = (ratio) => bandOf(ratio, BAND_EDGES);

/* The one breakpoint, shared with styles.css (tests/js/breakpoint.test.mjs). */
const NARROW_QUERY = "(max-width: 767.98px)";
const narrowMedia = window.matchMedia(NARROW_QUERY);
const isNarrow = () => narrowMedia.matches;

const TRACK_LABELS = {
  violent: "violent",
  non_violent: "non-violent",
};

/* Feature property names per track, so the map reads whichever is selected
   without refetching -- both ship on every feature. The h* pair is the same
   measure at the hour and weekday the layer was fetched for (the city's now).
   Switching track stays a repaint. */
const TRACK_PROPS = {
  violent: {
    pct: "safety_violent", tier: "stier_violent",
    hpct: "hsafety_violent", htier: "hstier_violent",
    rate: "sm_violent", hrate: "hsm_violent",
    weighted: "sw_violent",
  },
  non_violent: {
    pct: "safety_nonviolent", tier: "stier_nonviolent",
    hpct: "hsafety_nonviolent", htier: "hstier_nonviolent",
    rate: "sm_nonviolent", hrate: "hsm_nonviolent",
    weighted: "sw_nonviolent",
  },
};

/* Where each track's multiple of its city average is stored on a feature,
   once rateCity has worked it out (-1: the neutral fill). */
const RATIO_PROPS = { violent: "ratio_violent", non_violent: "ratio_nonviolent" };

const state = {
  /* Every served city's record from /api/v1/cities, bounding boxes included.
     All of them are on the map at once. */
  cities: [],
  /* The city under the middle of the map (updateFocus): the one address search,
     directions and the compass work in. Not a filter -- every city's cells are
     drawn whichever this is. */
  city: null,
  cityRecord: null,
  /* city id -> { res, hour, day, features, seq, abort }: each city's own layer,
     fetched for its own clock (cityNow) and rated against itself (rateCity). */
  layers: new Map(),
  /* The cell size the zoom calls for (resForZoom). Cities off screen stay at 8. */
  res: 8,
  track: "violent",
  // Show tiles: off paints the cells invisible (cellsOpacity), but they stay
  // on the map, so hovering and clicking one still opens its popup.
  tiles: true,
  selected: null,
  hovered: null,
  // Every loaded city's features, merged, and h3 -> properties over them.
  features: [],
  featureIndex: new Map(),
  // {lat, lng} once the visitor shares a location; the compass reads from it.
  userLocation: null,
  // The open cell's detail payload, so a track switch re-reads rather than refetches.
  detail: null,
  refreshStamp: null,
  framed: false,
};

const nf = new Intl.NumberFormat("en-US");
const $ = (id) => document.getElementById(id);

const cityById = (id) => state.cities.find((c) => c.source_id === id) ?? null;

/** The hour block and weekday it is now in a city: what its cells are coloured
    for. On the city's own clock (web/hours.js), never the browser's. */
const cityNow = (city) => ({ hour: cityHour(city?.timezone), day: cityDay(city?.timezone) });

/** The city a cell on the map belongs to. */
const cellCity = (h3Index) => cityById(state.featureIndex.get(h3Index)?.city) ?? state.cityRecord;

/** The cell size a city's loaded layer is at, or the zoom's while it loads. */
const layerRes = (cityId) => state.layers.get(cityId)?.res ?? state.res;

/* ------------------------------------------------------------------ basemap */

/* Carto's light and dark basemaps, one per theme, from the hosts the CSP in
   safety/api/security.py already allows (nothing new is loaded from anywhere
   else). Same glyphs and sprite family, so the city labels look the same. */
const CARTO_STYLES = {
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};
const RASTER_TILES = { light: "light_all", dark: "dark_all" };

// Used only if the vector style is unreachable, so the page still renders a map.
const rasterFallback = (theme) => ({
  version: 8,
  sources: {
    carto: {
      type: "raster",
      tiles: ["a", "b", "c"].map((s) => `https://${s}.basemaps.cartocdn.com/${RASTER_TILES[theme]}/{z}/{x}/{y}.png`),
      tileSize: 256,
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
    },
  },
  layers: [{ id: "carto", type: "raster", source: "carto" }],
});

/* A style fetched once per theme, so switching back and forth is instant. */
const styleCache = new Map();

async function resolveStyle(theme = "light") {
  const key = CARTO_STYLES[theme] ? theme : "light";
  if (styleCache.has(key)) return structuredClone(styleCache.get(key));
  try {
    const response = await fetch(CARTO_STYLES[key]);
    if (response.ok) {
      const style = await response.json();
      styleCache.set(key, style);
      return structuredClone(style);
    }
  } catch {
    /* fall through */
  }
  return rasterFallback(key);
}

let map;

/* ------------------------------------------------------------- colour scale */

/**
 * What a cell is coloured by, on one track: the smoothed severity-weighted rate
 * at the city's current hour and weekday (the h* properties, ranked by the
 * server), and which cells take a colour from the ramp at all.
 *
 * `fallback` is the all-hours rate, for a cell with nothing of this track
 * reported at this hour. The hourly rate is the all-hours one times an hour
 * factor, so its tier 0 is the all-hours tier 0: a cell with nothing reported at
 * any hour. Its rate is still a ranking, built from its neighbours and the city
 * baseline, so it takes that smoothed rate rather than going grey -- as does a
 * cell with no hourly row at all (a city whose time-of-day layer is not built
 * yet). Only a cell with no rate anywhere stays in the neutral fill.
 */
function paintedMeasure(track = state.track) {
  const props = TRACK_PROPS[track];
  return {
    valueProp: props.hrate,
    tier: props.htier,
    // Nothing of this kind reported is a separate state, not the safe end of
    // the scale: an absence of reports is not evidence of safety.
    isPainted: (p) => (p[props.htier] ?? 0) !== 0,
    fallback: { valueProp: props.rate, tier: props.tier },
  };
}

/** One city's averages for a measure and its fallback. */
function measureDomain(measure, features) {
  const { valueProp, isPainted, fallback } = measure;
  const own = valueDomain(features, valueProp, isPainted, true);
  // The average is taken over cells with reports of their own, so borrowing
  // cells are placed on the scale without moving it.
  const ranked = (p) => (p[fallback.tier] ?? 0) !== 0;
  const overall = valueDomain(features, fallback.valueProp, ranked, true);
  if (!own && !overall) return null;
  return { own, overall };
}

/** A cell's multiple of its city average, or null for the neutral fill. */
function cellRatio(p, measure, domain) {
  if (!domain) return null;
  const { valueProp, isPainted, fallback } = measure;
  if (isPainted(p)) return domain.own ? (p[valueProp] ?? 0) / domain.own.average : null;
  if (fallback && domain.overall && typeof p[fallback.valueProp] === "number") {
    return p[fallback.valueProp] / domain.overall.average;
  }
  return null;
}

/**
 * Rate one city's cells against that city alone, on both tracks, and store the
 * result on each feature (RATIO_PROPS).
 *
 * Done per city, before the cities are merged into one source, because a
 * colour means "this many times the average of the city it is in". Reporting
 * practice differs too much between police departments for one city's cells to
 * be measured against another's. Both tracks are rated up front, so switching
 * track stays a repaint.
 */
function rateCity(features) {
  for (const track of Object.keys(TRACK_PROPS)) {
    const measure = paintedMeasure(track);
    const domain = measureDomain(measure, features);
    const prop = RATIO_PROPS[track];
    for (const feature of features) {
      const ratio = cellRatio(feature.properties, measure, domain);
      feature.properties[prop] = ratio === null ? -1 : ratio;
    }
  }
}

/** The selected track's ratio for a cell's properties, or null for the neutral fill. */
function ratioNow(p) {
  const ratio = p?.[RATIO_PROPS[state.track]];
  return typeof ratio === "number" && ratio >= 0 ? ratio : null;
}

/**
 * Paint expression: each cell's multiple of its own city's average, on the
 * hinged ramp (valueRampStops) -- green up to one and a half times, yellow
 * there, orange by double, red from triple. -1 marks the neutral fill.
 */
function fillColorExpression() {
  const ratio = ["coalesce", ["get", RATIO_PROPS[state.track]], -1];
  return [
    "case",
    ["<", ratio, 0], zeroFill(),
    ["interpolate", ["linear"], ratio, ...valueRampStops(VALUE_RAMP)],
  ];
}

/* ------------------------------------------------------------------ compass */

const COMPASS_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const COMPASS_NAMES = {
  N: "north", NE: "northeast", E: "east", SE: "southeast",
  S: "south", SW: "southwest", W: "west", NW: "northwest",
};

/* How far the compass looks, in rings of cells: roughly 1.5 km at every cell
   size, so switching resolution changes the detail and not the reach. */
const COMPASS_RINGS = { 8: 2, 9: 5, 10: 12 };

/* A sector has to beat the cell you are in by this much before the needle
   commits to it -- below that, the difference is noise in a smoothed rate. */
const COMPASS_MARGIN = 0.1;

/* Fewer cells than this in a sector (the city's edge, a river) is not enough
   to call it a direction. */
const COMPASS_MIN_CELLS = 2;

/**
 * The map's colour at a value, read off the same hinged ramp the fill uses, so
 * the compass wedges and the hexagons under them agree.
 */
function rampColorAt(value) {
  const stops = valueRampStops(VALUE_RAMP);
  if (value <= stops[0]) return stops[1];
  for (let i = 2; i < stops.length; i += 2) {
    if (value <= stops[i]) {
      // Nearest stop is close enough: adjacent steps are ~0.025 apart in L.
      return value - stops[i - 2] < stops[i] - value ? stops[i - 1] : stops[i + 1];
    }
  }
  return stops.at(-1);
}

/** Where the compass reads from: the visitor if they shared a location and
    are in the city on screen, else the middle of the map. A visitor in Austin
    looking at Chicago wants directions around what they are looking at, not a
    needle stuck on "no data" for a place a thousand miles away. */
function compassOrigin() {
  const here = state.userLocation;
  const city = cityAt(here ?? { lat: NaN, lng: NaN });
  // Wherever the visitor is, as long as they are on screen in a covered city.
  if (here && city && cityInView(city)) return { ...here, from: "your location" };
  const { lng, lat } = map.getCenter();
  return {
    lat, lng,
    from: "map centre",
    away: here ? (cityAt(here) ? "you are in another city" : "you are outside the covered cities") : null,
  };
}

/**
 * Mean painted value in each of eight compass sectors around the origin.
 *
 * Every cell within COMPASS_RINGS of the origin's cell is assigned to the
 * sector its centre falls in, measured as a bearing from the origin itself
 * (not from its cell's centre, which can be 250 m off at res 8). Cells nearer
 * the origin count for more -- it is a direction to walk, and the next block
 * matters more than one a mile out. Cells outside the city's layer are
 * skipped rather than read as zero: no data is not low crime.
 */
function compassSectors(origin) {
  const ratioOf = (p) => ratioNow(p) ?? 0;
  const byCell = state.featureIndex;
  const city = cityAt(origin);
  if (!city) return null;
  const res = layerRes(city.source_id);
  const home = h3.latLngToCell(origin.lat, origin.lng, res);
  const homeProps = byCell.get(home);
  if (!homeProps) return null;

  const cosLat = Math.cos((origin.lat * Math.PI) / 180);
  const sectors = COMPASS_POINTS.map(() => ({ sum: 0, weight: 0, cells: 0 }));

  for (const [ring, cells] of h3.gridDiskDistances(home, COMPASS_RINGS[res] ?? 2).entries()) {
    if (ring === 0) continue;
    for (const cell of cells) {
      const p = byCell.get(cell);
      // Only the origin's own city: two cities' ratios are not on one scale.
      if (!p || p.city !== city.source_id) continue;
      const [lat, lng] = h3.cellToLatLng(cell);
      const bearing =
        (Math.atan2((lng - origin.lng) * cosLat, lat - origin.lat) * 180) / Math.PI;
      const index = Math.round(((bearing + 360) % 360) / 45) % 8;
      const weight = 1 / ring;
      const sector = sectors[index];
      sector.sum += ratioOf(p) * weight;
      sector.weight += weight;
      sector.cells += 1;
    }
  }

  return {
    here: ratioOf(homeProps),
    sectors: sectors.map((s, i) => ({
      point: COMPASS_POINTS[i],
      mean: s.cells >= COMPASS_MIN_CELLS ? s.sum / s.weight : null,
    })),
  };
}

/* How far the compass reads, as the visitor is told it: COMPASS_RINGS keeps
   it at about this at every cell size. */
const COMPASS_REACH_KM = 1.5;

/** Where to point and how much lower it is, from compassSectors' result:
    null when nothing nearby is meaningfully lower than here. */
function compassPick(result) {
  if (!result) return { best: null, ranked: [] };
  const ranked = result.sectors
    .map((s, i) => ({ ...s, index: i }))
    .filter((s) => s.mean !== null)
    .sort((a, b) => a.mean - b.mean);
  const top = ranked[0];
  const best = top && top.mean < result.here * (1 - COMPASS_MARGIN) ? top : null;
  return { best, ranked };
}

/**
 * The rose as SVG: eight ring-shaped wedges in their sector's colour (the
 * map's ramp, so wedge and hexagons agree), the chosen one outlined, a short
 * needle from the hub toward it, and the visitor's blue dot in the middle.
 * `labels` writes N, NE, ... in the wedges (the big rose).
 */
function roseSvg(sectors, bestIndex, { labels = false } = {}) {
  const outer = 49;
  const inner = labels ? 16 : 14;
  const at = (radius, deg) => {
    const rad = ((deg - 90) * Math.PI) / 180;
    return [(radius * Math.cos(rad)).toFixed(2), (radius * Math.sin(rad)).toFixed(2)];
  };
  const wedge = (i) => {
    const [a0, a1] = [i * 45 - 22.5, i * 45 + 22.5];
    const [x0, y0] = at(inner, a0);
    const [x1, y1] = at(outer, a0);
    const [x2, y2] = at(outer, a1);
    const [x3, y3] = at(inner, a1);
    return `M${x0} ${y0} L${x1} ${y1} A${outer} ${outer} 0 0 1 ${x2} ${y2} L${x3} ${y3} A${inner} ${inner} 0 0 0 ${x0} ${y0} Z`;
  };
  const list = sectors ?? COMPASS_POINTS.map((point) => ({ point, mean: null }));
  const fills = list.map((s) => (s.mean === null ? zeroFill() : colorForRatio(s.mean)));
  // The chosen wedge last, so its outline is not painted over by a neighbour.
  const order = list.map((_, i) => i).sort((a, b) => (a === bestIndex) - (b === bestIndex));
  const needle = bestIndex === null || bestIndex === undefined ? "" : (() => {
    const [x, y] = at(inner + (labels ? 7 : 6), bestIndex * 45);
    return html`<line class="needle" x1="0" y1="0" x2="${x}" y2="${y}"></line>`;
  })();
  return html`${order.map((i) => html`<path class="wedge${i === bestIndex ? " is-best" : ""}" d="${wedge(i)}" fill="${fills[i]}"></path>`)}${
    labels ? list.map((s, i) => {
      const [x, y] = at((inner + outer) / 2 + 2, i * 45);
      return html`<text x="${x}" y="${y}">${s.point}</text>`;
    }) : ""}${needle}<circle class="hub" r="${inner - 2}"></circle><circle class="me" r="${labels ? 4.6 : 4}"></circle>`;
}

/** The roses turn against the map, so their N stays on real north. */
function turnRoses() {
  if (!map) return;
  const turn = `rotate(${-map.getBearing()}deg)`;
  document.querySelectorAll(".rose").forEach((rose) => { rose.style.transform = turn; });
}

/** "55% quieter", "20% busier", or the band's name from double on. */
function shortWords(ratio) {
  if (ratio >= 2) return bandName(band(ratio));
  const change = Math.round((ratio - 1) * 100);
  if (Math.abs(change) < 5) return "about average";
  return change < 0 ? `${-change}% quieter` : `${change}% busier`;
}

/** Point both roses at the quietest nearby sector, or say there isn't one. */
function renderCompass() {
  const box = $("compass");
  const ready = Boolean(map && state.featureIndex?.size);
  box.hidden = !ready;
  const origin = ready ? compassOrigin() : null;
  const result = ready ? compassSectors(origin) : null;
  const { best, ranked } = compassPick(result);
  const fromYou = origin?.from === "your location";
  const track = TRACK_LABELS[state.track];
  state.compass = { origin, result, best };

  // The headline both share.
  let headline;
  let lede;
  if (!ready) {
    headline = "Move the map over a city";
    lede = "The compass reads the hexagons around you, or the middle of the map.";
  } else if (!result) {
    headline = fromYou ? "You're just outside the data" : "Move the map over a city";
    lede = fromYou
      ? "Where you are is past the edge of this city's data, so there is nothing around you to compare."
      : "The middle of the map is not over a city's hexagons.";
  } else if (!best) {
    headline = result.here === 0 ? "It's already quiet here" : "No clearly calmer way";
    lede = result.here === 0
      ? `Within about a mile, nothing nearby has fewer ${track} incidents at this hour.`
      : `Within about a mile, no direction has meaningfully fewer ${track} incidents at this hour.`;
  } else {
    const drop = result.here > 0 ? Math.round((1 - best.mean / result.here) * 100) : null;
    headline = `Head ${COMPASS_NAMES[best.point]}`;
    lede = `Within about a mile of ${fromYou ? "you" : "the middle of the map"}, ${track} incidents at this hour are ` +
      `usually lowest in that direction${drop === null ? "" : `, about ${drop}% lower than ${fromYou ? "where you are" : "there"}`}.`;
  }

  // Small, on Explore.
  $("compass-rose").innerHTML = roseSvg(result?.sectors, best?.index);
  $("compass-readout").textContent = headline;
  $("compass-foot").textContent = !result ? "" : fromYou ? "From you, within about a mile" : "From the map centre, within about a mile";
  box.setAttribute("aria-label", `Want a calmer walk? ${headline}. Open Near me.`);
  box.dataset.state = best ? "point" : "none";

  // Big, on Near me.
  $("near-title").textContent = headline;
  $("near-lede").textContent = lede;
  $("near-rose").innerHTML = roseSvg(result?.sectors, best?.index, { labels: true });
  $("near-list").innerHTML = html`${ranked.slice(0, 3).map((s) => html`<li>
    <span class="near-badge" data-band="${band(s.mean)}" aria-hidden="true">${s.point}</span>
    <span class="near-name">${COMPASS_NAMES[s.point][0].toUpperCase()}${COMPASS_NAMES[s.point].slice(1)}</span>
    <span class="near-num"><span class="sr-only">${COMPASS_NAMES[s.point]}: </span><strong>${formatRatio(s.mean)}</strong><span class="near-words">${shortWords(s.mean)}</span></span>
  </li>`)}`;
  const cityName = cityAt(state.userLocation ?? { lat: NaN, lng: NaN })?.city_name;
  $("near-origin-text").textContent = !origin ? ""
    : fromYou ? `Reading from your location, since you're in ${cityName ?? "this city"}. If you move the map to somewhere else, we use the middle of the map instead.`
    : origin.away ? `Reading from the middle of the map, because ${origin.away}.`
    : "Reading from the middle of the map. Tap the location button to read from where you are.";
  turnRoses();
  renderNearOverlay();
}

/* ---- the compass drawn on the map, on Near me */

const nearEmpty = { type: "FeatureCollection", features: [] };

function addNearLayers() {
  if (map.getSource("near")) return;
  map.addSource("near", { type: "geojson", data: nearEmpty });
  const layers = map.getStyle().layers;
  const lastRoad = layers.findLastIndex((layer) => /^(tunnel|road|bridge|highway)/.test(layer.id));
  const before = layers.slice(lastRoad + 1).find((layer) => layer.type === "symbol")?.id;
  map.addLayer({
    id: "near-sectors", type: "fill", source: "near",
    filter: ["==", ["get", "kind"], "sector"],
    paint: { "fill-color": ["get", "color"], "fill-opacity": ["case", ["get", "best"], 0.4, 0.14] },
  }, before);
  map.addLayer({
    id: "near-spokes", type: "line", source: "near",
    filter: ["==", ["get", "kind"], "spoke"],
    paint: { "line-color": themeColor("--panel", "#ffffff"), "line-width": 2.5, "line-opacity": 0.9 },
  }, before);
  map.addLayer({
    id: "near-ring", type: "line", source: "near",
    filter: ["==", ["get", "kind"], "ring"],
    paint: { "line-color": primaryColor(), "line-width": 2.5, "line-dasharray": [2, 2] },
  }, before);
  map.addLayer({
    id: "near-arrow", type: "line", source: "near",
    filter: ["==", ["get", "kind"], "arrow"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": themeColor("--text", "#3a2b26"), "line-width": 6 },
  }, before);
  map.addLayer({
    id: "near-origin", type: "circle", source: "near",
    filter: ["==", ["get", "kind"], "origin"],
    paint: {
      "circle-radius": 9,
      "circle-color": themeColor("--me", "#4b7be5"),
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 3,
    },
  }, before);
}

/** A point `km` from `origin` on a compass bearing (degrees from north). */
function offsetKm(origin, km, bearing) {
  const rad = (bearing * Math.PI) / 180;
  const lat = origin.lat + (km * Math.cos(rad)) / 111.32;
  const lng = origin.lng + (km * Math.sin(rad)) / (111.32 * Math.cos((origin.lat * Math.PI) / 180));
  return [lng, lat];
}

/** The circle the compass reads, its eight sectors tinted, and an arrow to
    the quietest -- shown with the Near me screen open. */
function renderNearOverlay() {
  const source = map?.getSource("near");
  if (!source) return;
  const view = document.body.dataset.view;
  const show = view === "near" && (!isNarrow() || document.body.classList.contains("near-on-map"));
  const c = state.compass;
  if (!show || !c?.result) {
    source.setData(nearEmpty);
    return;
  }
  const { origin, result, best } = c;
  const features = [];
  const ring = (from, to) => {
    const points = [];
    for (let deg = from; deg <= to; deg += 3) points.push(offsetKm(origin, COMPASS_REACH_KM, deg));
    return points;
  };
  result.sectors.forEach((s, i) => {
    if (s.mean === null) return;
    const arc = ring(i * 45 - 22.5, i * 45 + 22.5);
    features.push({
      type: "Feature",
      properties: { kind: "sector", color: colorForRatio(s.mean), best: best?.index === i },
      geometry: { type: "Polygon", coordinates: [[[origin.lng, origin.lat], ...arc, [origin.lng, origin.lat]]] },
    });
  });
  for (let i = 0; i < 8; i++) {
    features.push({
      type: "Feature",
      properties: { kind: "spoke" },
      geometry: { type: "LineString", coordinates: [[origin.lng, origin.lat], offsetKm(origin, COMPASS_REACH_KM, i * 45 + 22.5)] },
    });
  }
  features.push({ type: "Feature", properties: { kind: "ring" }, geometry: { type: "LineString", coordinates: ring(0, 360) } });
  if (best) {
    const bearing = best.index * 45;
    const tip = offsetKm(origin, COMPASS_REACH_KM * 0.72, bearing);
    const tipPoint = { lng: tip[0], lat: tip[1] };
    features.push({
      type: "Feature",
      properties: { kind: "arrow" },
      geometry: {
        type: "MultiLineString",
        coordinates: [
          [[origin.lng, origin.lat], tip],
          [offsetKm(tipPoint, 0.22, bearing + 155), tip, offsetKm(tipPoint, 0.22, bearing - 155)],
        ],
      },
    });
  }
  features.push({ type: "Feature", properties: { kind: "origin" }, geometry: { type: "Point", coordinates: [origin.lng, origin.lat] } });
  source.setData({ type: "FeatureCollection", features });
}

/* --------------------------------------------------------------- data fetch */

/* Every city is on the map at once, over the last year -- the only data there
   is. Each city's layer is its own request: one city, the cell size the zoom
   calls for, and the hour and weekday it is now *there*. Its colours are worked
   out against that city alone (rateCity) before the layers are merged into the
   one `cells` source. */

/* At most this many /cells requests in flight. The API allows a burst per
   client; a refresh of every city is several requests at once, and spreading
   them keeps a zoom in the middle of one from tripping the limit. */
const LAYER_CONCURRENCY = 3;

/** Whether a city's box overlaps the map on screen. */
function cityInView(city) {
  if (!map || !Number.isFinite(city.bbox_west)) return false;
  const b = map.getBounds();
  return !(
    city.bbox_east < b.getWest() || city.bbox_west > b.getEast() ||
    city.bbox_north < b.getSouth() || city.bbox_south > b.getNorth()
  );
}

/** The cell size a city's layer should be at: the zoom's while the city is on
    screen, the coarse one while it is not -- a zoomed-in view of one city does
    not fetch the fine cells of five others. */
function wantedRes(city) {
  return state.res !== 8 && cityInView(city) ? state.res : 8;
}

function showLoading(text = "Loading…", isError = false) {
  $("loading").textContent = text;
  $("loading").classList.toggle("is-error", isError);
  $("loading").hidden = false;
}

function hideLoading() {
  $("loading").hidden = true;
  $("loading").textContent = "Loading…";
  $("loading").classList.remove("is-error");
}

/** Fetch one city's layer as it should be now. True if it was stored; false if
    a newer request for the same city overtook it. */
async function loadCityLayer(city) {
  const id = city.source_id;
  const layer = state.layers.get(id) ?? { features: null, seq: 0, abort: null };
  state.layers.set(id, layer);
  const res = wantedRes(city);
  const { hour, day } = cityNow(city);
  const seq = ++layer.seq;
  layer.abort?.abort();
  const controller = (layer.abort = new AbortController());

  const params = new URLSearchParams({
    city: id, res: String(res), hour: String(hour), day: String(day),
  });
  const response = await fetch(`${API}/cells?${params}`, { signal: controller.signal });
  if (!response.ok) {
    throw Object.assign(new Error(`cells request failed: ${response.status}`), {
      status: response.status,
    });
  }
  const collection = await response.json();
  // A newer request for this city has been issued since; its result wins.
  if (seq !== layer.seq) return false;
  for (const feature of collection.features) feature.properties.city = id;
  rateCity(collection.features);
  Object.assign(layer, { res, hour, day, features: collection.features, meta: collection.metadata });
  return true;
}

/** Whether a city's layer is missing, at the wrong cell size, or coloured for
    an hour or weekday that has passed there. */
function layerStale(city) {
  const layer = state.layers.get(city.source_id);
  if (!layer?.features) return true;
  const { hour, day } = cityNow(city);
  return layer.res !== wantedRes(city) || layer.hour !== hour || layer.day !== day;
}

/**
 * Bring every city's layer up to date and repaint once.
 *
 * Only the cities that need it are fetched (layerStale), unless `force` -- a
 * pipeline refresh, when every city reloads. `quiet` keeps the loading pill
 * down: the hourly roll-over and the refresh poll should not flash it.
 */
let layersRun = Promise.resolve();
function refreshLayers(options = {}) {
  // One pass at a time: a zoom during a refresh queues behind it rather than
  // racing it, and each pass re-reads what is stale when it starts.
  layersRun = layersRun.then(() => refreshLayersNow(options), () => refreshLayersNow(options));
  return layersRun;
}

async function refreshLayersNow({ force = false, quiet = false } = {}) {
  const todo = state.cities.filter((city) => force || layerStale(city));
  if (!todo.length) return;
  // The visible ones first, so what is on screen paints soonest.
  todo.sort((a, b) => Number(cityInView(b)) - Number(cityInView(a)));
  if (!quiet) {
    $("map").classList.add("is-refetching");
    showLoading();
  }

  const failures = [];
  let changed = false;
  const queue = [...todo];
  const worker = async () => {
    while (queue.length) {
      const city = queue.shift();
      try {
        if (await loadCityLayer(city)) {
          changed = true;
          // Paint as each city arrives rather than after the slowest.
          mergeLayers();
        }
      } catch (error) {
        if (error.name !== "AbortError") failures.push(error);
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(LAYER_CONCURRENCY, todo.length) }, worker));

  $("map").classList.remove("is-refetching");
  if (failures.length && !quiet) {
    console.error(failures);
    showLoading(
      failures.some((e) => e.status === 429)
        ? "Too many requests — wait a moment, then move the map to retry."
        : "Could not load some of the map. Try again shortly.",
      true
    );
  } else if (!quiet || $("loading").classList.contains("is-error")) {
    hideLoading();
  }
  if (!changed) return;
  keepSelection();
}

/** Every city's features into the one source, and the fill and compass with them. */
function mergeLayers() {
  // The pointer can sit still across a layer swap, so mouseleave never fires
  // and the hover highlight would stay on a cell from the previous layer.
  if (state.hovered) {
    map.setFeatureState({ source: "cells", id: state.hovered }, { hover: false });
    state.hovered = null;
  }
  const features = [];
  for (const city of state.cities) {
    const layer = state.layers.get(city.source_id);
    if (layer?.features) features.push(...layer.features);
  }
  state.features = features;
  state.featureIndex = new Map(features.map((f) => [f.properties.h3, f.properties]));
  map.getSource("cells")?.setData({ type: "FeatureCollection", features });
  map.setPaintProperty("cells-fill", "fill-color", fillColorExpression());
  if (state.selected && state.featureIndex.has(state.selected)) {
    map.setFeatureState({ source: "cells", id: state.selected }, { selected: true });
  }
  renderCompass();
}

/** After a reload: the open cell stays open, or moves to the cell at the same
    spot when its city changed cell size under it. */
function keepSelection() {
  const h3Index = state.selected;
  if (!h3Index) return;
  if (state.featureIndex.has(h3Index)) {
    selectCell(h3Index);
    return;
  }
  const [lat, lng] = h3.cellToLatLng(h3Index);
  closeDetail();
  const city = cityAt({ lat, lng });
  if (!city) return;
  const cell = h3.latLngToCell(lat, lng, layerRes(city.source_id));
  if (state.featureIndex.has(cell)) selectCell(cell);
}

/** The served cities, from /api/v1/cities. Empty while no city has finished its
    first load -- the progress bar says why. */
async function loadCities() {
  const response = await fetch(`${API}/cities`);
  if (!response.ok) throw new Error(`cities request failed: ${response.status}`);
  const { cities } = await response.json();
  state.cities = cities;
  renderCityMarkers();
  renderCitySelect();
  return cities;
}

/** The city picker: the chip in the search box, which opens the Cities
    screen, and that screen's cards. Picking one only moves the map
    (switchCity); the one the map is on is marked. */
function renderCitySelect() {
  const current = state.city ?? DEFAULT_CITY;
  const record = cityById(current);
  $("city-chip").textContent = record?.city_name ?? "";
  $("city-chip").setAttribute("aria-label", record ? `${record.city_name}: choose a city` : "Choose a city");
  $("city-chip").hidden = !record;
  renderCities();
}

/** "Friday 9 PM": the hour and day the map is coloured for in the city on
    screen. On the city's clock, not the browser's. */
function renderTimeChips() {
  const city = state.cityRecord ?? cityById(DEFAULT_CITY);
  if (!city) return;
  const { hour, day } = cityNow(city);
  const when = `${dayName(day)} ${hourName(hour)}`;
  document.querySelectorAll(".time-now").forEach((el) => {
    el.textContent = el.closest(".see-card") ? `Right now · ${when}` : when;
  });
}

/** Frame one city from its own stored bounding box. */
function frameCity(city, { animate = false } = {}) {
  if (!Number.isFinite(city?.bbox_west)) return;
  map.fitBounds(
    [
      [city.bbox_west, city.bbox_south],
      [city.bbox_east, city.bbox_north],
    ],
    { padding: { top: 28, bottom: 28, left: 28, right: 28 }, duration: animate ? 700 : 0 }
  );
  state.framed = true;
}

/**
 * The city the page is "in": under the middle of the map, else the nearest.
 *
 * It scopes the things that work inside one city -- address search, directions
 * and the compass -- and names the page. It never filters the map: every
 * city's cells are drawn whichever this is.
 */
function updateFocus() {
  if (!map || !state.cities.length) return;
  const { lng, lat } = map.getCenter();
  const point = { lat, lng };
  const city = cityAt(point) ?? nearestCity(point)?.city ?? null;
  if (!city || city.source_id === state.city) return;
  state.city = city.source_id;
  state.cityRecord = city;
  renderCitySelect();
  renderTimeChips();
  document.title = `${city.city_name} — reported-incident risk`;
  $("map").setAttribute(
    "aria-label",
    `Map of every covered city, hexagonal cells shaded by relative risk; centred on ${city.city_name}`
  );
}

/* ---- city markers: where the cities are, when zoomed out too far to see cells */

/** Below this zoom a cell is a speck, so each city is marked by name instead. */
const CITY_MARKER_MAX_ZOOM = 8;

function cityMarkerData() {
  return {
    type: "FeatureCollection",
    features: state.cities
      .filter((c) => Number.isFinite(c.center_lng) && Number.isFinite(c.center_lat))
      .map((c) => ({
        type: "Feature",
        id: c.source_id,
        geometry: { type: "Point", coordinates: [c.center_lng, c.center_lat] },
        properties: { id: c.source_id, name: c.city_name },
      })),
  };
}

function addCityLayers() {
  if (map.getSource("cities")) return;
  map.addSource("cities", { type: "geojson", data: cityMarkerData() });
  map.addLayer({
    id: "city-dots",
    type: "circle",
    source: "cities",
    maxzoom: CITY_MARKER_MAX_ZOOM,
    paint: {
      "circle-radius": 8,
      "circle-color": primaryColor(),
      "circle-stroke-color": themeColor("--panel", "#ffffff"),
      "circle-stroke-width": 3,
    },
  });
  map.addLayer({
    id: "city-labels",
    type: "symbol",
    source: "cities",
    maxzoom: CITY_MARKER_MAX_ZOOM,
    layout: {
      "text-field": ["get", "name"],
      "text-offset": [0, 1.2],
      "text-anchor": "top",
      "text-size": 13,
      // The basemap's own font stack; the raster fallback has none and simply
      // shows the dots.
      "text-font": ["Montserrat Medium", "Open Sans Regular"],
    },
    paint: { "text-color": themeColor("--text", "#3a2b26"), "text-halo-color": themeColor("--panel", "#ffffff"), "text-halo-width": 1.5 },
  });
}

function wireCityLayerEvents() {
  map.on("click", "city-dots", (event) => {
    const id = event.features?.[0]?.properties?.id;
    if (id) switchCity(id);
  });
  map.on("mouseenter", "city-dots", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "city-dots", () => { map.getCanvas().style.cursor = ""; });
}

function renderCityMarkers() {
  map?.getSource("cities")?.setData(cityMarkerData());
}

/* ---- the first load's progress bar (GET /api/v1/progress, web/progress.js) */

const PROGRESS_POLL_MS = 10_000;
let progressTimer = null;

/** The load's state in three places: the full bar on the Cities screen, a
    line on the Explore card, and a small pill over the map. */
function renderProgress(body) {
  state.progress = body;
  const view = progressView(body);
  $("load-progress").hidden = !view.show;
  $("see-progress").hidden = !view.show;
  $("load-pill").hidden = !view.show;
  renderCitySelect();
  if (!view.show) return;
  $("lp-text").textContent = view.text;
  $("lp-detail").textContent = view.detail;
  $("lp-fill").style.width = `${view.percent}%`;
  const bar = $("lp-bar");
  bar.setAttribute("aria-valuenow", String(Math.round(view.percent)));
  bar.setAttribute("aria-valuetext", view.text);

  // "Austin is still loading, about 25 min left." -- one city by name, or
  // how many, with the one estimate the endpoint gives for the whole load.
  const waiting = (body.cities ?? []).filter((c) => !c.ready);
  const eta = formatEta(body.eta_seconds);
  const who = waiting.length === 1 ? `${waiting[0].name} is` : `${waiting.length} cities are`;
  const line = `${who} still loading${eta ? `, ${eta}` : ""}.`;
  $("see-progress-text").textContent = line;
  $("see-progress-fill").style.width = `${view.percent}%`;
  $("load-pill").textContent = `Loading · ${Math.floor(view.percent)}%`;
  $("load-pill").setAttribute("aria-label", `${view.text}. Open Cities for detail.`);
}

/**
 * The Cities screen: every served city, and any still on its first load,
 * with its local time and whether it is ready. A ready city is a button that
 * flies the map there (switchCity); a loading one shows its own progress.
 */
function renderCities() {
  const grid = $("city-grid");
  if (!grid) return;
  const progress = new Map((state.progress?.cities ?? []).map((c) => [c.id, c]));
  const served = [...state.cities].map((city) => ({ id: city.source_id, name: city.city_name, city }));
  for (const [id, c] of progress) if (!cityById(id)) served.push({ id, name: c.name, city: null });
  // Ready cities by name, then any still on their first load.
  const loadingRank = ({ id, city }) => Number(!city || Boolean(progress.get(id) && !progress.get(id).ready));
  served.sort((a, b) => loadingRank(a) - loadingRank(b) || a.name.localeCompare(b.name));
  const now = new Date();
  const label = (city) => city?.windows?.[0]?.label ?? "Last 12 months";

  grid.innerHTML = html`${served.map(({ id, name, city }) => {
    const p = progress.get(id);
    const loading = !city || (p && !p.ready);
    const viewing = !loading && id === state.city;
    const percent = loading ? Math.max(0, Math.min(100, p?.percent ?? 0)) : 100;
    const state_ = viewing
      ? html`<span class="city-state is-viewing">Viewing</span>`
      : loading
        ? html`<span class="city-state is-loading">Loading ${Math.floor(percent)}%</span>`
        : html`<span class="city-state is-ready">Ready</span>`;
    const what = loading && p?.phase ? PROGRESS_PHASES[p.phase] ?? p.phase : null;
    return html`<li><button type="button" class="city-card${loading ? " is-loading" : " is-ready"}"
        data-city="${id}" ${loading ? html`disabled` : ""} aria-current="${String(viewing)}">
      <span class="city-head"><span class="hex" aria-hidden="true"></span>
        <span class="city-name"><strong>${name}</strong><span>${
          city ? `${cityTimeLabel(city.timezone, now)} local time` : "Not on the map yet"}</span></span>
        ${state_}</span>
      <span class="bar" aria-hidden="true"><i style="width:${percent}%"></i></span>
      <span class="city-foot">${loading
        ? `${what ? `${what[0].toUpperCase()}${what.slice(1)}` : "Waiting to start"}`
        : label(city)}</span>
    </button></li>`;
  })}`;

  const waiting = served.filter(({ id, city }) => !city || (progress.get(id) && !progress.get(id).ready));
  $("cities-tip").hidden = waiting.length === 0;
  if (waiting.length) {
    const names = waiting.map((w) => w.name).join(", ");
    $("cities-tip-text").textContent = waiting.length === 1
      ? `${names} is still loading for the first time. You can use the other cities while it finishes, and it will appear on its own.`
      : `${names} are still loading for the first time. You can use the other cities while they finish, and each will appear on its own.`;
  }
}

/**
 * Poll the load's progress while one runs, and pick up each city as it becomes
 * ready. Stops when nothing is loading; the minute tick (watchForRefresh) starts
 * it again if a later deploy begins another load.
 */
async function pollProgress() {
  clearTimeout(progressTimer);
  progressTimer = null;
  let body = null;
  try {
    const response = await fetch(`${API}/progress`, { cache: "no-store" });
    if (response.ok) body = await response.json();
  } catch {
    /* transient: try again on the next poll */
  }
  if (body) {
    renderProgress(body);
    const served = new Set(state.cities.map((c) => c.source_id));
    const arrived = body.cities.some((c) => c.ready && !served.has(c.id));
    if (arrived) await onCitiesChanged();
  }
  if (!body || body.active) progressTimer = setTimeout(pollProgress, PROGRESS_POLL_MS);
}

/** A city finished loading (or the list otherwise changed): show it. */
async function onCitiesChanged() {
  const before = state.cities.length;
  try {
    await loadCities();
  } catch (error) {
    console.error(error);
    return;
  }
  if (!state.cities.length) return;
  if (!before) openOnFirstCity();
  else await refreshLayers();
}

/* --------------------------------------------------------------- the cell */

/**
 * Open a cell's card: its rating now, and its hour and weekday shapes.
 * `place` names it when the cell was reached through an address -- a search,
 * a dropped pin -- and is what "Take me here" routes to.
 */
async function selectCell(h3Index, { place = null } = {}) {
  if (state.selected && state.selected !== h3Index) {
    map.setFeatureState({ source: "cells", id: state.selected }, { selected: false });
  }
  if (state.selected !== h3Index || place) state.cardPlace = place;
  state.selected = h3Index;
  map.setFeatureState({ source: "cells", id: h3Index }, { selected: true });

  const { hour } = cityNow(cellCity(h3Index));
  const detail = await fetch(`${API}/cells/${encodeURIComponent(h3Index)}?hour=${hour}`)
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
  // A later click may have moved on while this one was in flight.
  if (!detail || state.selected !== h3Index) return;
  // Kept so switching track re-reads the profiles without a refetch, the same
  // way the map repaints from properties it already holds.
  state.detail = detail;
  showCellPopup(h3Index, detail);
}

function closeDetail() {
  removeCellPopup();
  if (state.selected) {
    map.setFeatureState({ source: "cells", id: state.selected }, { selected: false });
    state.selected = null;
  }
  state.detail = null;
  state.cardPlace = null;
}

/** The card's clock: the cell's city's exact time now, "10:13 PM EDT". */
function renderCityClock() {
  const clock = document.querySelector("#cell-card .cell-time");
  if (clock && state.selected) {
    clock.textContent = `${cityTimeLabel(cellCity(state.selected)?.timezone, new Date(), { zoneName: true })} there now`;
  }
  renderTimeChips();
}

/**
 * Every minute, on the minute: keep each city coloured for the hour it is now
 * there. A city whose hour (or day) has rolled over since its layer was fetched
 * reloads quietly (layerStale), and an open popup redraws so its "now" bar
 * moves with it. Skipped while the tab is hidden; coming back catches up at once.
 */
function watchCityClock() {
  const tick = async () => {
    if (document.hidden || !state.cities.length) return;
    const open = state.selected;
    const before = open ? cityNow(cellCity(open)) : null;
    await refreshLayers({ quiet: true });
    renderCityClock();
    if (open && state.selected === open && state.detail) {
      const after = cityNow(cellCity(open));
      if (after.hour !== before.hour || after.day !== before.day) showCellPopup(open, state.detail);
    }
  };
  setTimeout(() => {
    tick();
    setInterval(tick, 60_000);
  }, 60_000 - (Date.now() % 60_000));
  document.addEventListener("visibilitychange", tick);
}

/** "38%": the size of a fractional change, sign carried by the words around it. */
const pctOf = (change) => `${Math.abs(Math.round(change * 100))}%`;

/**
 * The 12-hour outlook in one sentence, plain text: "Heads up: it usually gets
 * quieter here from 1 AM to 4 AM, about 35% lower than now."
 *
 * Compared with right now, not the day's average: at 9 AM every stretch in
 * the next twelve hours is daytime and above average, so an average-based
 * figure would call the "quieter" stretch "24% above average". When even the
 * quietest stretch is still busier than the day as a whole, it says so.
 */
function windowSentence(win) {
  if (win.startsNow) {
    return `Right now is usually the quietest stretch of the next 12 hours here, until ${hourName(win.end)}.`;
  }
  if (!win.quieter) return "No meaningfully quieter stretch is usual here in the next 12 hours.";
  const still = win.aboveAverage ? ", though still busier than an average hour of the day" : "";
  return `Heads up: it usually gets quieter here from ${hourName(win.start)} to ${hourName(win.end)}, ` +
    `about ${pctOf(win.vsNow)} lower than now${still}.`;
}

/* ------------------------------------------------------- cell size by zoom */

/* The cell size follows the zoom: resolution 8 (~0.3 mi cells) while
   SCALE_MAX_WIDTH pixels -- the scale bar's own width -- span 0.6214 mi or
   more, resolution 9 (~0.1 mi) from there in. Measured the way MapLibre's
   ScaleControl measures, across the middle of the map. */
const SCALE_MAX_WIDTH = 110;
const METERS_PER_MILE = 1609.344;
const FINE_RES_BELOW_MILES = 1000 / METERS_PER_MILE; // 0.6214

function resForZoom() {
  const y = map.getContainer().clientHeight / 2;
  const meters = map.unproject([0, y]).distanceTo(map.unproject([SCALE_MAX_WIDTH, y]));
  return meters / METERS_PER_MILE < FINE_RES_BELOW_MILES ? 9 : 8;
}

/** After the camera moves: the zoom's cell size for the cities on screen, and
    any city that has come into view at the fine size. keepSelection carries an
    open cell across a change of size. */
async function syncResToZoom() {
  if (!state.cities.length) return;
  state.res = resForZoom();
  await refreshLayers();
}

/* ---------------------------------------------------------------- cell card */

/* The whole cell view: the relative rating now, and the cell's day and week
   against themselves -- a card along the bottom on desktop, a bottom sheet on
   a phone. */

/** "#rrggbb" for a multiple of the city average, by the same linear
    interpolation the fill's `interpolate` expression makes over
    valueRampStops, so a swatch matches the cell it belongs to. */
function colorForRatio(ratio) {
  if (ratio === null || ratio === undefined || ratio < 0) return zeroFill();
  const stops = valueRampStops(VALUE_RAMP);
  const parse = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  for (let k = 2; k < stops.length; k += 2) {
    const [r0, c0, r1, c1] = stops.slice(k - 2, k + 2);
    if (ratio > r1) continue;
    const t = r1 === r0 ? 1 : Math.max(0, (ratio - r0) / (r1 - r0));
    const [a, b] = [parse(c0), parse(c1)];
    return `#${a.map((x, i) => Math.round(x + (b[i] - x) * t).toString(16).padStart(2, "0")).join("")}`;
  }
  return stops[stops.length - 1];
}

/* The face's mouth per band, on a 46-unit grid with the eyes at x 17 and
   29, so every face is symmetric about x 23: a smile, flat, a frown. */
const FACE_MOUTHS = {
  calm: "M14.5 26.5 Q23 35 31.5 26.5",
  busier: "M16 29 H30",
  busy: "M14.5 32 Q23 24.5 31.5 32",
  very: "M14.5 32 Q23 24.5 31.5 32",
  none: "M16 29 H30",
};

/** A face for a band, as SVG (styled by .face in styles.css). */
function faceSvg(b) {
  return html`<svg viewBox="0 0 46 46"><circle cx="17" cy="18" r="3"></circle><circle cx="29" cy="18" r="3"></circle><path d="${FACE_MOUTHS[b] ?? FACE_MOUTHS.none}"></path></svg>`;
}

function removeCellPopup() {
  const card = $("cell-card");
  const hadFocus = card.contains(document.activeElement);
  card.hidden = true;
  card.classList.remove("is-expanded");
  $("cell-body").replaceChildren();
  if (hadFocus) $("map").focus?.();
}

/** "About half a mile across": a cell's size, as the help dialog words it. */
const cellSizeWords = (h3Index) => (h3.getResolution(h3Index) >= 9 ? "about a fifth of a mile across" : "about half a mile across");

function showCellPopup(h3Index, detail) {
  const props = state.featureIndex.get(h3Index);
  const ratio = ratioNow(props);
  const b = band(ratio);
  const city = cellCity(h3Index);
  const zone = city?.timezone;
  const { hour: nowHour, day: nowDay } = cityNow(city);
  const track = TRACK_LABELS[state.track];
  const place = state.cardPlace;

  // The rating is the same multiple of its city's average the cell's colour
  // comes from: the smoothed, severity-weighted rate the ranking is built on,
  // at the hour and weekday it is now in that city.
  const windowLabel = detail.window_label.toLowerCase();
  const when = `${dayName(nowDay)} ${hourName(nowHour)}`;
  const words = ratio === null
    ? `No ${track} rating for this spot at ${when}`
    : `${ratioWords(ratio)}`;

  // The day: this cell's own 24 hours on this track, with now in orange and
  // the quietest three-hour stretch ahead (hours.js) in green -- only when it
  // is worth pointing at. Needs 48 reports across the day (hourVsUsual), the
  // same floor the outlook uses; below that the chart is skipped.
  const counts = detail.hour_profile?.[state.track];
  const enough = counts ? hourVsUsual(counts, nowHour) !== null : false;
  const win = enough ? quietestWindow(counts, nowHour) : null;
  const safer = new Set(win && (win.startsNow || win.quieter) ? win.blocks : []);
  const span = (h) => `${hourName(h)} – ${hourName(h + 1)}`;
  const max = Math.max(...(counts ?? []), 1);
  const today = enough
    ? html`<section class="cell-today" aria-labelledby="cell-today-title">
        <h3 id="cell-today-title">How it usually goes today</h3>
        <div class="hours" role="img" aria-label="Reported ${track} incidents here by hour of the day; now is ${hourName(nowHour)}">${counts.map((n, h) => {
          const height = Math.max((n / max) * 100, n > 0 ? 6 : 0);
          return html`<i style="height:${height}%"${safer.has(h) ? html` data-safer` : ""}${h === nowHour ? html` data-selected` : ""} title="${span(h)}: ${nf.format(n)}"></i>`;
        })}</div>
        <div class="hours-axis" aria-hidden="true"><span>12 AM</span><span>6 AM</span><span>12 PM</span><span>6 PM</span><span>11 PM</span></div>
        <p class="tip tip-inline"><span class="buddy" aria-hidden="true"><svg><use href="#i-shield"/></svg></span><span>${windowSentence(win)}</span></p>
        <p class="cell-foot">Orange: now in ${city?.city_name ?? "the city"}.${safer.size ? " Green: the quieter stretch." : ""} The usual pattern here, not a forecast.</p>
      </section>`
    : html`<section class="cell-today">
        <h3>How it usually goes today</h3>
        <p class="note">${counts && counts.some((n) => n > 0)
          ? "Fewer than 48 reports here across the day, so there is no hour-by-hour chart. The week pattern is below."
          : "No reports here with a recorded hour."}</p>
      </section>`;

  // The week: this cell's incidents on each weekday, today in orange.
  const days = detail.day_profile?.[state.track];
  const week = days && days.some((n) => n > 0)
    ? (() => {
        const most = Math.max(...days, 1);
        const vs = dayVsUsual(days, nowDay);
        const sentence = !vs
          ? "Too few reports here to compare days of the week."
          : vs.direction === "flat"
            ? `${dayName(nowDay)}s here are about an average day.`
            : `${dayName(nowDay)}s are ${vs.direction === "up" ? "a bit busier" : "a bit quieter"} here, about ` +
              `${pctOf(vs.change)} ${vs.direction === "up" ? "above" : "below"} an average day.`;
        return html`<section class="cell-week cell-more" aria-labelledby="cell-week-title">
          <h3 id="cell-week-title">Across the week</h3>
          <div class="hours days" role="img" aria-label="Reported ${track} incidents here by day of the week; today is ${dayName(nowDay)}">${days.map((n, d) => {
            const height = Math.max((n / most) * 100, n > 0 ? 6 : 0);
            return html`<i style="height:${height}%"${d === nowDay ? html` data-selected` : ""} title="${dayName(d)}: ${nf.format(n)}"></i>`;
          })}</div>
          <div class="days-axis" aria-hidden="true">${days.map((_, d) => html`<span${d === nowDay ? html` class="is-today"` : ""}>${dayName(d)[0]}</span>`)}</div>
          <p class="cell-foot">${sentence} Patterns, not predictions.</p>
        </section>`;
      })()
    : "";

  const title = place?.label ?? "This hexagon";
  const sub = place?.dropped ? "Nearest address" : place ? (city?.city_name ?? "") : `${cellSizeWords(h3Index)}, ${city?.city_name ?? ""}`;
  $("cell-body").innerHTML = html`<section class="cell-summary" data-band="${b}">
      ${place?.dropped ? html`<p class="cell-kicker">Dropped pin</p>` : ""}
      <h2 class="cell-title" id="cell-title">${title}</h2>
      <p class="cell-sub">${sub}</p>
      <div class="rating" data-band="${b}">
        <span class="face" data-band="${b}" aria-hidden="true">${faceSvg(b)}</span>
        <span>
          <span class="rating-value">${ratio === null ? "—" : formatRatio(ratio)}</span>
          <span class="rating-words">${words}</span>
        </span>
      </div>
      <p class="cell-sub cell-meta">${track[0].toUpperCase()}${track.slice(1)} incidents · ${when} · ${windowLabel}
        <br><span class="cell-time"></span></p>
      <div class="cell-actions">
        <button type="button" class="btn btn-primary" data-card="to">Take me here</button>
        <button type="button" class="btn btn-ghost" data-card="from">${place?.dropped ? "Start from here" : "Start here"}</button>
      </div>
      <button type="button" class="link-btn cell-expand" aria-expanded="false">See today and the week</button>
    </section>${today}${week}`;

  const card = $("cell-card");
  const opening = card.hidden;
  card.style.setProperty("--cell-color", colorForRatio(ratio));
  card.hidden = false;
  renderCityClock();
  // Only when the card first opens: a track switch or the minute tick
  // redraws it in place and should not move the map.
  if (opening) keepCellInView(h3Index);
}

/**
 * Bring the selected hexagon into the part of the map the card and the
 * other floating controls leave free, if it is not already there. On a
 * phone the card is a sheet over the bottom of the map, and a tapped
 * hexagon below the sheet's top edge would otherwise sit behind its own
 * card. Measured with clearOfControls, which counts the open card.
 */
function keepCellInView(h3Index) {
  if (!map || nav.active) return;
  const [lat, lng] = h3.cellToLatLng(h3Index);
  const at = map.project([lng, lat]);
  const pad = clearOfControls();
  const width = map.getContainer().clientWidth;
  const height = map.getContainer().clientHeight;
  const margin = 24;
  const inside =
    at.x >= pad.left + margin && at.x <= width - pad.right - margin &&
    at.y >= pad.top + margin && at.y <= height - pad.bottom - margin;
  if (inside) return;
  // easeTo's offset places the centre this far from the map's middle: the
  // middle of the free area.
  const offset = [
    (pad.left + (width - pad.right)) / 2 - width / 2,
    (pad.top + (height - pad.bottom)) / 2 - height / 2,
  ];
  map.easeTo({ center: [lng, lat], offset, duration: 450 });
}

/** The address a card stands for: its place when it has one, else the
    nearest address to the middle of its hexagon. */
async function cardPlace() {
  if (state.cardPlace) return state.cardPlace;
  if (!state.selected) return null;
  const [lat, lng] = h3.cellToLatLng(state.selected);
  const { place } = await placeAt({ lat, lng });
  return place;
}

function wireCellCard() {
  const card = $("cell-card");
  $("cell-close").onclick = () => closeDetail();
  card.addEventListener("click", async (event) => {
    const expand = event.target.closest(".cell-expand");
    if (expand) {
      const open = card.classList.toggle("is-expanded");
      expand.setAttribute("aria-expanded", String(open));
      expand.textContent = open ? "Show less" : "See today and the week";
      return;
    }
    const action = event.target.closest("[data-card]");
    if (!action) return;
    action.disabled = true;
    const place = await cardPlace().catch(() => null);
    action.disabled = false;
    if (!place) return;
    if (action.dataset.card === "to") routeTo(place);
    else routeFrom(place);
  });

  // The sheet's handle on a phone: drag down to close, up to show more; a
  // tap toggles. The close button and "See today and the week" do the same
  // from a keyboard.
  let drag = null;
  card.addEventListener("pointerdown", (event) => {
    if (!isNarrow() || !event.target.closest(".sheet-handle, .cell-summary h2, .cell-kicker")) return;
    drag = { y: event.clientY, id: event.pointerId };
  });
  card.addEventListener("pointerup", (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    const dy = event.clientY - drag.y;
    drag = null;
    if (dy > 60) closeDetail();
    else if (dy < -30 || Math.abs(dy) < 6) card.querySelector(".cell-expand")?.click();
  });
  card.addEventListener("pointercancel", () => { drag = null; });
}

/* ------------------------------------------------------------------ geolocate */

/** Great-circle distance in km. */
function distanceKm(a, b) {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLng = (b.lng - a.lng) * rad;
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
}

const inBbox = (city, { lat, lng }) =>
  Number.isFinite(city.bbox_west) &&
  lng >= city.bbox_west && lng <= city.bbox_east &&
  lat >= city.bbox_south && lat <= city.bbox_north;

/** How far a point is from a city's bounding box: 0 inside it, else to the
    nearest edge -- "40 km from Chicago" should mean from where its data
    starts, not from downtown. */
function bboxDistanceKm(city, point) {
  const nearest = {
    lat: Math.min(Math.max(point.lat, city.bbox_south), city.bbox_north),
    lng: Math.min(Math.max(point.lng, city.bbox_west), city.bbox_east),
  };
  return distanceKm(point, nearest);
}

/**
 * The served city a point falls in, or null.
 *
 * A bounding box is a rectangle, so it also takes in some ground just past
 * the city line. That is fine here: this only picks which city to open, and
 * the cell lookup afterwards is what decides whether there is data under the
 * point. Where two boxes overlap, the nearer centre wins.
 */
function cityAt(point) {
  const hits = state.cities.filter((city) => inBbox(city, point));
  hits.sort(
    (a, b) =>
      distanceKm(point, { lat: a.center_lat, lng: a.center_lng }) -
      distanceKm(point, { lat: b.center_lat, lng: b.center_lng })
  );
  return hits[0] ?? null;
}

/** The served city nearest a point, with its distance in km. */
function nearestCity(point) {
  let best = null;
  for (const city of state.cities) {
    if (!Number.isFinite(city.bbox_west)) continue;
    const km = bboxDistanceKm(city, point);
    if (!best || km < best.km) best = { city, km };
  }
  return best;
}

/** "3.4 mi", "25 mi": a distance given in km, as the page shows distances. */
const milesLabel = (km) => {
  const miles = km / 1.609344;
  return miles < 10 ? `${miles.toFixed(1)} mi` : `${nf.format(Math.round(miles))} mi`;
};

/**
 * The city to frame first, from a location the visitor has *already* allowed.
 *
 * Never prompts: a permission dialog the moment the page opens, before anyone
 * has asked for anything, is the wrong first impression for a safety map. Where
 * the browser has no grant on record this returns null at once and the page
 * frames DEFAULT_CITY. Bounded by a short timeout, because it sits
 * in front of the first paint.
 */
async function cityFromGrantedLocation() {
  try {
    if (!navigator.geolocation || !navigator.permissions?.query) return null;
    const permission = await navigator.permissions.query({ name: "geolocation" });
    if (permission.state !== "granted") return null;
    const position = await new Promise((resolve, reject) =>
      navigator.geolocation.getCurrentPosition(resolve, reject, {
        enableHighAccuracy: false,
        timeout: 4000,
        maximumAge: 10 * 60_000,
      })
    );
    const point = { lat: position.coords.latitude, lng: position.coords.longitude };
    state.userLocation = point;
    state.userFixAt = Date.now();
    showUserDot(point);
    followLocation();
    return cityAt(point)?.source_id ?? null;
  } catch {
    return null;
  }
}

/**
 * A note over the map about where the visitor is, replacing the old alert():
 * an alert blocks the page and offers no way forward. `action`, when given,
 * is {label, run} for one button -- usually "View <nearest city>".
 */
function showLocateNote(text, action = null) {
  $("locate-note-text").textContent = text;
  const go = $("locate-note-go");
  go.hidden = !action;
  if (action) {
    go.textContent = action.label;
    go.onclick = () => {
      hideLocateNote();
      action.run();
    };
  }
  $("locate-note").hidden = false;
}

function hideLocateNote() {
  $("locate-note").hidden = true;
}

/** Fly to a city. Every city is already on the map; this only moves the view,
    and resolves once the move has settled and its cells are loaded. */
async function switchCity(sourceId) {
  const city = cityById(sourceId);
  if (!city) return;
  const settled = new Promise((resolve) => map.once("moveend", resolve));
  frameCity(city, { animate: true });
  await settled;
  await resSync;
}

/* The visitor's blue dot, once they have shared a location. */
let userMarker = null;
function showUserDot(point) {
  if (!map || !point) return;
  if (!userMarker) {
    const el = document.createElement("div");
    el.className = "user-dot";
    el.setAttribute("aria-hidden", "true");
    el.append(document.createElement("span"));
    userMarker = new maplibregl.Marker({ element: el });
  }
  userMarker.setLngLat([point.lng, point.lat]).addTo(map);
  // While navigating, the arrow (nav.marker) is the visitor instead.
  userMarker.getElement().hidden = nav.active;
}

/**
 * Keep the compass (and the blue dot) on the visitor as they move, once they
 * have shared a location. The map and the open card stay where the visitor
 * put them.
 */
let locationWatch = null;
function followLocation() {
  if (locationWatch !== null || !navigator.geolocation?.watchPosition) return;
  locationWatch = navigator.geolocation.watchPosition(
    (position) => {
      state.userLocation = {
        lat: position.coords.latitude,
        lng: position.coords.longitude,
      };
      state.userFixAt = Date.now();
      showUserDot(state.userLocation);
      renderCompass();
    },
    () => { /* keep the last fix */ },
    { enableHighAccuracy: true, maximumAge: 15000 }
  );
}

/* Within this far of a city's data, being outside it still gets a "View
   <city>" button; farther, the map goes back to the city being looked at. */
const NEAR_CITY_MILES = 50;

/**
 * The Locate button: ask for the visitor's location (only now, on their
 * tap -- never on page load), then show it. Nothing about the location leaves
 * the page: the cell is worked out here from the coordinates (design doc
 * S10), and the city from bounding boxes already in hand.
 */
async function locateMe() {
  const button = $("btn-locate");
  if (!navigator.geolocation) {
    showLocateNote("This browser does not share a location.");
    return;
  }
  // The city on screen when the visitor asked: where the map goes back to if
  // they turn out to be nowhere near any city.
  const before = state.city ?? DEFAULT_CITY;
  button.disabled = true;
  button.classList.add("is-busy");
  button.setAttribute("aria-busy", "true");
  let point;
  try {
    point = await currentPosition();
  } catch (error) {
    showLocateNote(
      error?.code === 1
        ? "Location is off for this site. Allow it in your browser's settings to see where you are."
        : `Could not read your location${error?.message ? `: ${error.message}` : ""}.`
    );
    return;
  } finally {
    button.disabled = false;
    button.classList.remove("is-busy");
    button.removeAttribute("aria-busy");
  }
  state.userLocation = point;
  state.userFixAt = Date.now();
  showUserDot(point);
  followLocation();
  await goToLocation(point, before);
}

/** Show where the visitor is, or say plainly why the map cannot. */
async function goToLocation(point, before) {
  hideLocateNote();
  const home = cityAt(point);

  if (!home) {
    const near = nearestCity(point);
    const miles = near ? near.km / 1.609344 : Infinity;
    if (near && miles <= NEAR_CITY_MILES) {
      showLocateNote(
        `You're outside the cities this map covers. The nearest is ${near.city.city_name}, about ${milesLabel(near.km)} away.`,
        near.city.source_id === state.city
          ? null
          : { label: `View ${near.city.city_name}`, run: () => switchCity(near.city.source_id) }
      );
      renderCompass();
      return;
    }
    // Nowhere near any city: say so, then put the map back on the city the
    // visitor was looking at (Philadelphia if none).
    const back = cityById(before) ?? cityById(DEFAULT_CITY) ?? state.cities[0];
    showLocateNote(
      `You're outside the cities this map covers` +
        (near ? `; the nearest, ${near.city.city_name}, is about ${milesLabel(near.km)} away` : "") +
        (back ? `. Back to ${back.city_name} for now.` : ".")
    );
    if (back) await switchCity(back.source_id);
    renderCompass();
    return;
  }

  await flyAndSettle([point.lng, point.lat], Math.max(map.getZoom(), 14));
  const cell = coveredCell(point);
  if (!cell) {
    // Inside the box but past the edge of the data: across the city line, or
    // somewhere the source publishes nothing.
    showLocateNote(`You're in ${home.city_name}, but just outside the area its data covers.`);
    renderCompass();
    return;
  }
  selectCell(cell, { place: { ...point, label: "You are here" } });
  renderCompass();
}

/** The buddy's tip, once per visit: how to drop a pin. */
let pinTipShown = false;
function pinTip() {
  if (pinTipShown) return;
  pinTipShown = true;
  showLocateNote(
    isNarrow()
      ? "Pin dropped! Press and hold anywhere on the map to check a spot, or to start directions from it."
      : "Pin dropped! Right-click anywhere on the map to check a spot, or to start directions from it."
  );
}

/* ---------------------------------------------------- address search + route */

/* Testing UI: find an address in the city on screen, pin it and open its
   cell's popup, then route to it -- from the visitor's location or a second
   address -- with the same POST /api/v1/directions the main page uses. The
   formatting and the map features are web/directions.js's, copied here as
   directions.js.

   Addresses and points go to the server in POST bodies, never in a URL, and
   the server makes every provider call: nothing here talks to a third party,
   so the content security policy is unchanged. */

const search = { place: null, marker: null, seq: 0 };

const dir = {
  open: false,
  status: null,
  // The start; the destination is always search.place.
  from: null,
  // Bumped on every new start, so a location lookup that finishes after a
  // newer choice can tell, and drop its answer.
  fromSeq: 0,
  mode: "walk",
  result: null,
  selected: 0,
  seq: 0,
  abort: null,
  markers: { from: null, to: null },
  // Results already shown, so going back to a mode does not ask again --
  // transit plans come out of a small daily allowance shared by everyone.
  cache: new Map(),
};

const DIR_CACHE_MAX = 20;
// A "leave now" answer is about now; past this it is asked for again.
const DIR_NOW_TTL_MS = 5 * 60_000;
const MODE_NAMES = { drive: "Driving", walk: "Walking", bike: "Cycling", transit: "Transit" };
const UNAVAILABLE = {
  budget: "has reached today's limit for this site",
  unavailable: "is unavailable right now",
};
// Close enough to read the street the address is on.
const SEARCH_ZOOM = 15;

/** Where an element is on screen, or null when it is not shown. */
function shownRect(id) {
  const el = $(id);
  return el && el.getClientRects().length ? el.getBoundingClientRect() : null;
}

/** Camera padding that keeps a target clear of whatever floats over the
    map: the rail or tab bar, the open panel or sheet, the search and the
    Explore card. Measured, so it follows the layout rather than repeating it. */
function clearOfControls() {
  const gap = 24;
  const width = window.innerWidth;
  const height = window.innerHeight;
  const pad = { top: gap, bottom: gap, left: gap, right: gap };
  const below = (id) => {
    const r = shownRect(id);
    if (r) pad.top = Math.max(pad.top, r.bottom + gap);
  };
  const above = (id) => {
    const r = shownRect(id);
    if (r) pad.bottom = Math.max(pad.bottom, height - r.top + gap);
  };
  if (!isNarrow()) {
    pad.left = (shownRect("rail")?.right ?? 0) + gap;
    for (const id of ["dir-panel", "near-panel", "cities-panel"]) {
      const r = shownRect(id);
      if (r) pad.left = Math.max(pad.left, r.right + gap);
    }
    const see = shownRect("see-card");
    if (see) pad.right = width - see.left + gap;
    below("search-field");
    above("cell-card");
  } else {
    below("search-field");
    below("dir-top");
    above("rail");
    above("cell-card");
    // The panel itself has no box on a phone; its two halves do.
    const sheet = $("dir-panel").hidden ? null : $("dir-panel").querySelector(".dir-sheet");
    if (sheet?.getClientRects().length) pad.bottom = Math.max(pad.bottom, height - sheet.getBoundingClientRect().top + gap);
  }
  // Never so much that nothing is left to frame.
  pad.top = Math.min(pad.top, height * 0.45);
  pad.bottom = Math.min(pad.bottom, height * 0.45);
  pad.left = Math.min(pad.left, width * 0.6);
  return pad;
}

async function postJson(path, body, signal) {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    /* an empty or non-JSON error body */
  }
  return {
    ok: response.ok,
    status: response.status,
    payload,
    retryAfter: Number(response.headers.get("Retry-After")) || null,
  };
}

/** A server error as one sentence. FastAPI's own 422s carry a list of field
    errors, not prose; those read as a generic line. */
function errorText(result) {
  const detail = result.payload?.detail;
  if (typeof detail === "string") return detail;
  if (result.status === 429) return "Too many requests just now; wait a moment and try again.";
  return "That could not be loaded. Try again shortly.";
}

function setStatus(id, text, isError = false) {
  const el = $(id);
  el.textContent = text;
  el.classList.toggle("is-error", isError);
}

async function loadDirectionsStatus() {
  try {
    const response = await fetch(`${API}/directions/status`);
    if (!response.ok) return;
    dir.status = await response.json();
  } catch {
    return;
  }
  $("search-field").hidden = false;
  syncModeTabs();
  renderDirAttribution();
}

/** Disable a mode the server says is down or out of allowance, with why. */
function syncModeTabs() {
  const modes = dir.status?.modes ?? {};
  const tabs = [...$("dir-modes").querySelectorAll("[data-mode]")];
  const isDown = (mode) => modes[mode] && !modes[mode].available;
  // A selected mode that is down would still be what gets asked for: move to
  // the first one that is up (or stay, if none is).
  if (isDown(dir.mode)) {
    const up = tabs.find((tab) => !isDown(tab.dataset.mode));
    if (up) dir.mode = up.dataset.mode;
  }
  tabs.forEach((tab) => {
    const info = modes[tab.dataset.mode];
    const down = info && !info.available;
    tab.disabled = Boolean(down);
    tab.title = down ? `${MODE_NAMES[tab.dataset.mode]} ${UNAVAILABLE[info.reason] ?? UNAVAILABLE.unavailable}` : "";
    tab.setAttribute("aria-selected", String(tab.dataset.mode === dir.mode));
  });
  const geocode = dir.status?.geocode;
  if (geocode && !geocode.available) {
    setStatus(
      "search-status",
      `Address search ${UNAVAILABLE[geocode.reason] ?? UNAVAILABLE.unavailable}. You can still paste a lat, lng.`
    );
  }
}

function renderDirAttribution() {
  const a = dir.status?.attribution;
  if (!a) return;
  const sources = safeHttpUrl(a.transit_sources_url);
  $("dir-attribution").innerHTML = html`
    Routing: ${a.routing} · Map data ${a.map_data} · Address search: ${a.geocoding} ·
    Transit: ${a.transit}${sources ? html` (<a href="${sources}" target="_blank" rel="noopener">data sources</a>)` : ""}
  `;
}

/** The selected track's multiple of the city average at a point, from the
    cells on the map: null for the no-data fill, undefined where no loaded
    cell covers it. */
function ratioAtPoint(point) {
  if (!point || !Number.isFinite(point.lat) || !Number.isFinite(point.lng)) return undefined;
  const city = cityAt(point);
  if (!city) return undefined;
  const props = state.featureIndex.get(h3.latLngToCell(point.lat, point.lng, layerRes(city.source_id)));
  return props ? ratioNow(props) : undefined;
}

/** One word for a chip, in the band's own terms: the green band ("typical or
    quieter") says which of the two. */
function chipWord(ratio) {
  const b = band(ratio);
  if (b === "calm") return ratio < 0.95 ? "Quieter" : "Typical";
  return b === "none" ? "No reports" : bandName(b);
}

/* ---- address boxes: an ARIA combobox each */

/**
 * Suggestions under `input` as the visitor types: places from /geocode in the
 * city on screen, or a typed "lat, lng" as it is. `onChoose` gets
 * {lat, lng, label}; `statusId` is where "no match" and errors go.
 */
function wireCombo(input, list, { onChoose, onType, statusId }) {
  let options = [];
  let active = -1;
  let abort = null;

  const render = (next) => {
    options = next;
    active = -1;
    // Numbered, each with how its hexagon compares now -- read from the
    // cells already on the map, so there is no request per suggestion. A
    // place whose hexagon is not loaded gets no chip rather than a guess.
    list.innerHTML = html`${next.map((o, i) => {
      const ratio = ratioAtPoint(o.point ?? o);
      const b = ratio === undefined ? "none" : band(ratio);
      return html`<li role="option" id="${list.id}-opt-${i}" data-index="${i}" aria-selected="false">
        <span class="opt-num" data-band="${b}" aria-hidden="true">${i + 1}</span>
        <span class="opt-text"><span class="opt-title">${o.title}</span>${o.subtitle ? html`<span class="opt-sub">${o.subtitle}</span>` : ""}</span>
        ${ratio === undefined ? "" : html`<span class="band-chip plain" data-band="${b}">${chipWord(ratio)}<span class="sr-only">, ${ratio === null ? "nothing reported" : formatRatio(ratio)}</span></span>`}
      </li>`;
    })}`;
    list.hidden = next.length === 0;
    input.setAttribute("aria-expanded", String(next.length > 0));
    input.removeAttribute("aria-activedescendant");
  };

  const move = (step) => {
    if (!options.length) return;
    active = (active + step + options.length) % options.length;
    list.querySelectorAll("[role=option]").forEach((li, i) => {
      li.setAttribute("aria-selected", String(i === active));
      if (i === active) li.scrollIntoView({ block: "nearest" });
    });
    input.setAttribute("aria-activedescendant", `${list.id}-opt-${active}`);
  };

  const choose = (index) => {
    const option = options[index];
    if (!option) return;
    render([]);
    input.value = option.label ?? option.title;
    onChoose(option.point ?? { lat: option.lat, lng: option.lng, label: option.label });
  };

  const lookup = debounce(async (q) => {
    abort?.abort();
    const point = latLngFromString(q);
    if (point) {
      render([{ title: `${point.lat}, ${point.lng}`, subtitle: "Coordinates", point: { ...point, label: `${point.lat}, ${point.lng}` } }]);
      return;
    }
    if (q.length < 3 || dir.status?.geocode?.available === false) {
      render([]);
      return;
    }
    const controller = new AbortController();
    abort = controller;
    let result;
    try {
      result = await postJson("/geocode", { q, city: state.city, limit: 5 }, controller.signal);
    } catch (error) {
      if (error.name !== "AbortError") setStatus(statusId, "Address search could not be reached.", true);
      return;
    }
    if (controller.signal.aborted) return;
    if (!result.ok) {
      setStatus(statusId, errorText(result), true);
      return;
    }
    const found = result.payload.results ?? [];
    render(found);
    const city = state.cityRecord?.city_name ?? "this city";
    setStatus(statusId, found.length ? "" : `No match in ${city}. Try more of the address, or paste a lat, lng.`);
  }, 350);

  input.addEventListener("input", () => {
    onType?.();
    lookup(input.value.trim());
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      move(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "Enter") {
      if (!list.hidden) {
        event.preventDefault();
        choose(Math.max(0, active));
      }
    } else if (event.key === "Escape" && !list.hidden) {
      event.stopPropagation();
      render([]);
    }
  });
  // After a click on an option has had its mousedown.
  input.addEventListener("blur", () => setTimeout(() => render([]), 150));
  list.addEventListener("mousedown", (event) => {
    const li = event.target.closest("[role=option]");
    if (!li) return;
    event.preventDefault();
    choose(Number(li.dataset.index));
  });
}

/* ---- the searched address: a pin and its cell's popup */

function searchPin() {
  const el = document.createElement("div");
  el.className = "search-pin";
  el.setAttribute("aria-hidden", "true");
  return new maplibregl.Marker({ element: el, anchor: "center" });
}

/** Resolves once the camera has stopped, and any cell-size change that move
    set off has loaded its layer. */
let resSync = null;
function flyAndSettle(center, zoom) {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      // zoomend (and with it syncResToZoom) fires before moveend.
      Promise.resolve(resSync).then(resolve, resolve);
    };
    // A camera already where it is asked to be may not fire moveend.
    const timer = setTimeout(() => {
      map.off("moveend", done);
      done();
    }, 2500);
    map.once("moveend", done);
    // An offset, not padding: padding passed to a camera move stays on the
    // map and would then add to every later fitBounds.
    const pad = clearOfControls();
    const offset = [(pad.left - pad.right) / 2, (pad.top - pad.bottom) / 2];
    map.flyTo({ center, zoom, offset, duration: 900 });
  });
}

/** The city on screen, if `point` is in it. */
function cityOnScreen(point) {
  const city = cityAt(point);
  return city && city.source_id === state.city ? city : null;
}

/** The cell `point` is in, if the city's data covers it. Inside the city's
    box but past the edge of its data -- across the city line, or somewhere
    the source publishes nothing -- there is none. */
function coveredCell(point) {
  const city = cityAt(point);
  if (!city) return null;
  const cell = h3.latLngToCell(point.lat, point.lng, layerRes(city.source_id));
  return state.featureIndex.has(cell) ? cell : null;
}

/** Why `point` cannot be used, or null if it can. `what` names it in the
    message ("That address"). */
function pointProblem(point, what) {
  const city = cityOnScreen(point);
  if (!city) return `${what} is outside ${state.cityRecord?.city_name ?? "this city"}.`;
  if (!coveredCell(point)) return `${what} is just outside the area ${city.city_name}'s data covers.`;
  return null;
}

function refuseSearch(message) {
  closeDirections();
  setStatus("search-status", message, true);
}

/** Pin `place`, fly to it and open its cell's card -- if it is a cell of
    the city on screen. */
async function showPlace(place) {
  const seq = ++search.seq;
  clearSearchPin();
  // The cell is checked after the flight, at the cell size it lands on.
  if (!cityOnScreen(place)) return refuseSearch(pointProblem(place, "That address"));
  setStatus("search-status", "");
  closeDetail();
  await flyAndSettle([place.lng, place.lat], Math.max(map.getZoom(), SEARCH_ZOOM));
  if (seq !== search.seq) return;

  const cell = coveredCell(place);
  if (!cell) return refuseSearch(pointProblem(place, "That address"));
  pinPlace(place, cell);
}

/** Right-click (press and hold on a phone) on the map. With directions open
    it sets the start (pinClickedStart). Otherwise it drops a pin where it
    is, with no camera move, and opens the dropped pin's card with the
    nearest address -- a way to find an address by pointing at it. */
async function pinClickedPoint(lngLat) {
  const point = { lat: lngLat.lat, lng: lngLat.lng };
  // With directions open it fills the trip: the destination while there is
  // none (after "Start here", say), else the start, as it always has.
  if (dir.open) return search.place ? pinClickedStart(point) : pinClickedDestination(point);
  const seq = ++search.seq;
  clearSearchPin();
  const problem = pointProblem(point, "That point");
  if (problem) return refuseSearch(problem);
  const cell = coveredCell(point);

  // The pin goes down at once; the address follows it.
  closeDetail();
  search.marker = searchPin().setLngLat([point.lng, point.lat]).addTo(map);
  $("search-input").value = "";
  setStatus("search-status", "Finding the nearest address…");
  // The point keeps where it was clicked; the address only names it.
  const { place } = await placeAt(point);
  if (seq !== search.seq) return;
  setStatus("search-status", "");
  $("search-input").value = place.label;
  pinPlace({ ...place, dropped: true }, cell);
  pinTip();
}

/** Right-click with directions open: the same, for the start. Its pin goes
    down at once, the start box fills with the nearest address, and the route
    is asked for. */
async function pinClickedStart(point) {
  const seq = ++dir.fromSeq;
  const problem = pointProblem(point, "That start");
  if (problem) return setStatus("dir-status", problem, true);

  clearResult();
  dir.markers.from?.remove();
  dir.markers.from = endMarker("from").setLngLat([point.lng, point.lat]).addTo(map);
  $("dir-from").value = "";
  setStatus("dir-status", "Finding the nearest address…");
  const { place } = await placeAt(point);
  // Something newer was chosen while the lookup ran.
  if (seq !== dir.fromSeq) return;
  // The panel was closed, or a start typed, while it ran: drop the answer,
  // and with it the pin, back to whatever start there was.
  if (!dir.open || $("dir-from").value) {
    setStatus("dir-status", "");
    placeMarkers();
    return;
  }
  setFrom(place);
}

/** Right-click / press-and-hold with directions open and no destination
    yet: the same as pinClickedStart, for the destination. */
async function pinClickedDestination(point) {
  const seq = ++search.seq;
  const problem = pointProblem(point, "That place");
  if (problem) return setStatus("dir-status", problem, true);
  closeDetail();
  dir.markers.to?.remove();
  dir.markers.to = endMarker("to").setLngLat([point.lng, point.lat]).addTo(map);
  $("dir-to").value = "";
  setStatus("dir-status", "Finding the nearest address…");
  const { place } = await placeAt(point);
  if (seq !== search.seq) return;
  // Closed, or a destination typed, while the lookup ran: drop the answer.
  if (!dir.open || $("dir-to").value) {
    setStatus("dir-status", "");
    placeMarkers();
    return;
  }
  setTo(place);
}

/** Make `place` the searched address: its pin and its cell's card. It is
    also the destination "Take me here" uses. */
function pinPlace(place, cell) {
  search.marker?.remove();
  search.place = place;
  search.marker = searchPin().setLngLat([place.lng, place.lat]).addTo(map);
  $("search-clear").hidden = false;
  selectCell(cell, { place });
}

function clearSearchPin() {
  search.place = null;
  search.marker?.remove();
  search.marker = null;
  $("search-clear").hidden = true;
}

/** Back to no address: the box, the pin, the card and any route. */
function resetSearch() {
  search.seq += 1;
  closeDirections();
  clearSearchPin();
  dir.fromSeq += 1;
  dir.from = null;
  $("dir-from").value = "";
  $("dir-to").value = "";
  $("search-input").value = "";
  setStatus("search-status", "");
  closeDetail();
}

/* ---- the directions panel */

/** Open the panel (setView("directions") calls this). With no destination
    yet, the To box is empty and gets the focus; with no start, From does. */
function openDirections({ focus = true } = {}) {
  dir.open = true;
  $("dir-panel").hidden = false;
  document.body.classList.add("dir-open");
  syncDirEnds();
  // The route takes the map: the card would sit on top of it, and the
  // searched pin becomes the destination's end pin.
  closeDetail();
  search.marker?.remove();
  placeMarkers();
  if (dir.result) {
    drawRoutes();
    $("dir-results").hidden = false;
  } else if (dir.from && search.place) {
    submitDirections();
  }
  if (focus) (search.place ? (dir.from ? $("dir-start") : $("dir-from")) : $("dir-to")).focus();
}

/** Close the panel. `toView` false: the caller is already switching to
    another section (setView), so this does not switch back to Explore. */
function closeDirections({ toView = true } = {}) {
  endNavigation();
  if (!dir.open) return;
  dir.open = false;
  $("dir-panel").hidden = true;
  document.body.classList.remove("dir-open");
  clearResult();
  clearRouteLayers();
  if (search.marker) search.marker.addTo(map);
  if (toView && document.body.dataset.view === "directions") setView("explore");
}

/** The From and To boxes show the trip's two ends. */
function syncDirEnds() {
  $("dir-from").value = dir.from?.label ?? $("dir-from").value;
  $("dir-to").value = search.place?.label ?? "";
  $("dir-to").closest(".dir-field").classList.toggle("is-set", Boolean(search.place));
}

/** Set (or clear, with null) the destination, and route once there is a
    start too. */
function setTo(place) {
  search.place = place;
  syncDirEnds();
  clearResult();
  placeMarkers();
  if (place && dir.from) submitDirections();
}

/** "Take me here": the place becomes the destination and Directions opens. */
function routeTo(place) {
  search.place = place;
  $("search-input").value = place.label ?? "";
  $("search-clear").hidden = false;
  closeDetail();
  setView("directions");
  setTo(place);
  if (!dir.from) askForEnd("from");
}

/** "Start here": the place becomes the start and Directions opens. */
function routeFrom(place) {
  closeDetail();
  setView("directions");
  setFrom(place);
  if (!search.place) askForEnd("to");
}

/** Say how to pick the missing end of the trip. On a phone the map is the
    quicker way, so the box is not focused (that would raise the keyboard
    over it); on desktop the box gets the focus for typing. */
function askForEnd(end) {
  const what = end === "to" ? "where you're going" : "where you're starting";
  const hold = isNarrow() ? "press and hold anywhere" : "right-click anywhere";
  setStatus("dir-status", `Now pick ${what}: tap a hexagon on the map, ${hold}, or type it above.`);
  if (!isNarrow()) $(end === "to" ? "dir-to" : "dir-from").focus();
}

/** Swap the two ends. */
function swapEnds() {
  const from = dir.from;
  const to = search.place;
  dir.fromSeq += 1;
  dir.from = to;
  search.place = from;
  $("dir-from").value = to?.label ?? "";
  syncDirEnds();
  clearResult();
  placeMarkers();
  if (dir.from && search.place) submitDirections();
}

function clearResult() {
  // A new start, mode or destination is a different trip; a reroute is the
  // same one, from where the visitor now is.
  if (nav.active && !nav.rerouting) endNavigation();
  dir.seq += 1;
  dir.abort?.abort();
  dir.result = null;
  $("dir-results").hidden = true;
  setStatus("dir-status", "");
  $("dir-status").classList.remove("sr-only");
  clearRouteLayers({ keepMarkers: true });
}

function endMarker(end) {
  const el = document.createElement("div");
  // MapLibre positions the marker with its own transform, so the look goes
  // on an inner element.
  el.className = "route-pin-marker";
  const pin = document.createElement("div");
  pin.className = `route-pin route-pin-${end}`;
  el.append(pin);
  el.setAttribute("aria-hidden", "true");
  return new maplibregl.Marker({ element: el, anchor: "center" });
}

/** A pin at each end of the trip, where they are not already. */
function placeMarkers() {
  const ends = { from: dir.from, to: search.place };
  for (const end of ["from", "to"]) {
    dir.markers[end]?.remove();
    dir.markers[end] = ends[end] && dir.open
      ? endMarker(end).setLngLat([ends[end].lng, ends[end].lat]).addTo(map)
      : null;
  }
}

/** Set (or clear, with null) the start, and route once there is one. */
function setFrom(place) {
  dir.fromSeq += 1;
  dir.from = place;
  $("dir-from").value = place?.label ?? "";
  clearResult();
  placeMarkers();
  if (place && search.place) submitDirections();
}

/** Name a point (reverse geocode) and say which served city it is in. */
async function placeAt(point) {
  const result = await postJson("/geocode/reverse", point).catch(() => null);
  const label = `${point.lat.toFixed(5)}, ${point.lng.toFixed(5)}`;
  if (!result?.ok) return { place: { ...point, label }, city: cityAt(point)?.source_id ?? null };
  return {
    place: { ...point, label: result.payload.result?.label || label },
    city: result.payload.city,
  };
}

/** A start from the visitor's location or a typed lat, lng: kept only if it
    is in the city on screen -- a route never crosses between two. */
async function setFromPoint(point) {
  const seq = ++dir.fromSeq;
  setStatus("dir-status", "Looking up that point…");
  const { place, city } = await placeAt(point);
  // Something newer was chosen while the lookup ran.
  if (seq !== dir.fromSeq) return;
  if (city !== state.city) {
    const name = state.cityRecord?.city_name ?? "this city";
    setStatus(
      "dir-status",
      city ? `That start is in another city. Directions stay within ${name}.` : `That start is outside ${name}.`,
      true
    );
    return;
  }
  setFrom({ ...place, label: point.label === undefined ? `My location (${place.label})` : place.label });
}

/** A destination typed as lat, lng: the same check. */
async function setToPoint(point) {
  const { place, city } = await placeAt(point);
  if (city !== state.city) {
    const name = state.cityRecord?.city_name ?? "this city";
    setStatus("dir-status", city ? `That place is in another city. Directions stay within ${name}.` : `That place is outside ${name}.`, true);
    return;
  }
  setTo({ ...place, label: point.label ?? place.label });
}

/** The visitor's position: the watch's last fix while it is fresh (under
    30 s), else a new one. A second one-off request while a watch runs can
    sit until its timeout in some browsers, and the watch already has the
    answer. */
function currentPosition() {
  const fresh = locationWatch !== null && state.userLocation && Date.now() - (state.userFixAt ?? 0) < 30_000;
  return fresh ? Promise.resolve({ ...state.userLocation }) : getPosition();
}

function getPosition() {
  if (!navigator.geolocation) {
    return Promise.reject(new Error("This browser does not expose a location."));
  }
  return new Promise((resolve, reject) =>
    navigator.geolocation.getCurrentPosition(
      (position) => resolve({ lat: position.coords.latitude, lng: position.coords.longitude }),
      reject,
      { enableHighAccuracy: false, timeout: 10000 }
    )
  );
}

/* ---- the request */

async function submitDirections() {
  const from = dir.from;
  const to = search.place;
  if (!from || !to) return;
  const request = {
    from: { lat: from.lat, lng: from.lng },
    to: { lat: to.lat, lng: to.lng },
    mode: dir.mode,
  };

  const key = directionsKey(request);
  const kept = dir.cache.get(key);
  if (kept && Date.now() - kept.at < DIR_NOW_TTL_MS) {
    showResult(kept.result);
    return;
  }

  dir.abort?.abort();
  const controller = new AbortController();
  dir.abort = controller;
  const seq = ++dir.seq;
  setStatus("dir-status", `Finding ${MODE_NAMES[dir.mode].toLowerCase()} routes…`);
  let result;
  try {
    result = await postJson("/directions", request, controller.signal);
  } catch (error) {
    if (error.name !== "AbortError" && seq === dir.seq) {
      setStatus("dir-status", "Directions could not be reached. Check your connection.", true);
    }
    return;
  }
  if (seq !== dir.seq) return;
  if (!result.ok) {
    $("dir-results").hidden = true;
    clearRouteLayers({ keepMarkers: true });
    // Transit is one plan per visitor per 20 s: asked for again when it is
    // allowed, unless something changes first.
    if (result.payload?.code === "too_soon") {
      const wait = result.retryAfter ?? 5;
      setStatus("dir-status", `Transit can be asked for again in ${wait} s; it will be, automatically.`);
      setTimeout(() => {
        if (seq === dir.seq) submitDirections();
      }, wait * 1000);
      return;
    }
    setStatus("dir-status", errorText(result), true);
    // A mode that just ran out says so on its tab too.
    if (result.payload?.code === "budget_exhausted" || result.status === 503) loadDirectionsStatus();
    return;
  }
  dir.cache.set(key, { result: result.payload, at: Date.now() });
  if (dir.cache.size > DIR_CACHE_MAX) dir.cache.delete(dir.cache.keys().next().value);
  showResult(result.payload);
}

function showResult(result) {
  dir.result = result;
  // The top card: the safest route where there is one.
  dir.selected = routeCardModel(result, state.track)[0].index;
  renderDirections();
  // A reroute while navigating: follow the new route straight away.
  if (nav.active) {
    beginRoute(result.routes[dir.selected]);
    renderNav();
  }
  const bounds = routesBbox(result.routes);
  if (bounds) map.fitBounds(bounds, { padding: clearOfControls(), maxZoom: 16, duration: 700 });
  const n = result.routes.length;
  // Announced, not shown: the heading over the cards says the same.
  setStatus(
    "dir-status",
    `${n} ${MODE_NAMES[result.mode].toLowerCase()} route${n === 1 ? "" : "s"} found · ` +
      `fastest ${formatDuration(result.routes[result.picks?.[state.track]?.fastest ?? 0].duration_s)}`
  );
  $("dir-status").classList.add("sr-only");
}

/* The card labels, in UI 5's words. The model's roles (routeCardModel) are
   about reported activity, never safety (design doc S13); "calmer" says the
   same thing the way the page talks. */
const ROLE_WORDS = {
  "Fastest and safest": "Fastest and calmest",
  Safest: "Calmer route",
  Fastest: "Fastest",
  Alternative: "Another option",
};
const MODE_VERBS = { walk: "walk", drive: "drive", bike: "bike" };

/** Great-circle length of a line of [lng, lat] points, in km. */
function lineKm(coords) {
  let km = 0;
  for (let i = 1; i < coords.length; i++) {
    km += distanceKm({ lng: coords[i - 1][0], lat: coords[i - 1][1] }, { lng: coords[i][0], lat: coords[i][1] });
  }
  return km;
}

/** The route's stretches as bars, each as long as it is and coloured by its
    own multiple of the city average (route.activity[track].segments, the same
    the map draws). Neighbours in the same band are joined. */
function stretchBars(activity) {
  const bars = [];
  for (const seg of activity?.segments ?? []) {
    const ratio = seg.ratio === null ? null : seg.ratio;
    const km = lineKm(seg.coordinates ?? []);
    if (!(km > 0)) continue;
    const b = band(ratio);
    const last = bars.at(-1);
    if (last && last.band === b) {
      last.km += km;
      last.sum += (ratio ?? 0) * km;
    } else {
      bars.push({ band: b, km, sum: (ratio ?? 0) * km, rated: ratio !== null });
    }
  }
  return bars.map((bar) => ({ ...bar, color: bar.rated ? colorForRatio(bar.sum / bar.km) : zeroFill() }));
}

function renderDirections() {
  const result = dir.result;
  if (!result) return;
  const track = TRACK_LABELS[state.track];
  const cards = routeCardModel(result, state.track);
  const fastestIndex = result.picks?.[state.track]?.fastest ?? 0;
  const fastest = result.routes[fastestIndex];
  const n = cards.length;

  $("dir-results").hidden = false;
  const count = result.mode === "transit"
    ? `${n} transit option${n === 1 ? "" : "s"}`
    : `${n} way${n === 1 ? "" : "s"} to ${MODE_VERBS[result.mode] ?? "go"} there`;
  // The other routes are the dotted lines on the map, and tapping one picks
  // it too -- easier than scrolling the list on a phone.
  $("dir-count").innerHTML = n > 1
    ? html`${count}<span class="dir-hint">Tap one here, or its dotted line on the map.</span>`
    : html`${count}`;
  $("dir-track").textContent =
    `Compared on ${track} incidents, against ` +
    `${result.window?.label?.toLowerCase() ?? "the city"}` +
    (result.daily
      ? ", at the day and hour each stretch would be passed."
      : result.hourly ? ", at the hour each stretch would be passed." : ", all hours.");

  $("dir-cards").innerHTML = html`${cards.map((card) => {
    const route = result.routes[card.index];
    const activity = card.activity;
    const words = activitySentence(activity, track);
    const ratio = activity?.comparable ? activity.mean_ratio : null;
    const b = band(ratio);
    const legs = route.mode === "transit" ? route.legs.map(transitLegSummary) : [];
    const bars = stretchBars(activity);
    // The calmer card's own "why", from the two routes' numbers: how much
    // longer, and how much less reported activity than the fastest.
    const fastAct = fastest.activity?.[state.track];
    const why = card.role === "Safest" && fastAct?.comparable && ratio !== null && fastAct.mean_ratio > 0
      ? `${formatDuration(route.duration_s - fastest.duration_s)} longer, and passes about ` +
        `${Math.round((1 - ratio / fastAct.mean_ratio) * 100)}% less reported activity than the fastest way.`
      : null;
    const restText = words.detail.split(" · ").slice(1).join(" · ");
    const rest = restText ? `${restText[0].toUpperCase()}${restText.slice(1)}.` : "";
    return html`<li>
      <button type="button" class="route-card" data-index="${card.index}"
              aria-pressed="${String(card.index === dir.selected)}">
        <span class="route-top">
          <span class="route-role"${card.role === "Safest" || card.role === "Fastest and safest" ? html` data-band="calm"` : ""}>${ROLE_WORDS[card.role] ?? card.role}</span>
          <span class="route-ratio" data-band="${b}">${ratio === null ? "Not rated" : `${formatRatio(ratio)} city avg`}</span>
        </span>
        <span class="route-main"><strong>${card.duration}</strong><span>${card.distance}</span>${
          card.extra ? html`<span class="route-extra">${card.extra}</span>` : ""}${
          route.mode === "transit" && route.start_time
            ? html`<span>leaves ${clockIn(route.start_time, result.timezone)}</span>`
            : ""
        }</span>
        ${legs.length ? html`<span class="route-legs">${legs.map((leg) =>
          leg.kind === "ride"
            ? html`<span class="leg-chip" style="${leg.color ? `background:${leg.color};color:${leg.textColor}` : ""}">${leg.name}</span>`
            : html`<span class="leg-walk">${leg.text.split(" ")[0]}</span>`
        )}</span>` : ""}
        ${bars.length ? html`<span class="route-stretches" aria-hidden="true">${bars.map((bar) =>
          html`<i style="flex-grow:${bar.km.toFixed(3)};background:${bar.color}"></i>`)}</span>` : ""}
        <span class="route-detail">${why ?? `${words.headline}.`}${rest ? html`<br>${rest}` : ""}</span>
      </button>
    </li>`;
  })}`;

  // Why no slower route is the lower-activity pick -- only true, and only
  // shown, when the fastest is on top. The buddy says it.
  const fastestOnTop = cards[0].index === fastestIndex;
  const reason = fastestOnTop ? pickReason(result.picks?.[state.track], result.mode) ?? "" : "";
  $("dir-why").textContent = reason;
  $("dir-why-tip").hidden = !reason;
  $("dir-caveats").innerHTML = html`${(result.caveats ?? []).map((c) => html`<li>${c}</li>`)}`;
  renderSteps();
  drawRoutes();
}

function renderSteps() {
  const result = dir.result;
  const route = result.routes[dir.selected];
  if (route.mode === "transit") {
    $("dir-steps-title").textContent = "Itinerary";
    $("dir-steps").innerHTML = html`${route.legs.map((leg) => {
      const s = transitLegSummary(leg);
      const when = leg.start_time ? clockIn(leg.start_time, result.timezone) : "";
      if (s.kind === "street") {
        return html`<li class="step step-walk"><span class="step-time">${when}</span>
          <span>${s.text}${leg.to?.name ? html` to ${leg.to.name}` : ""}</span></li>`;
      }
      return html`<li class="step step-ride"><span class="step-time">${when}</span>
        <span><span class="leg-chip" style="${s.color ? `background:${s.color};color:${s.textColor}` : ""}">${s.name}</span>
        ${s.text} <span class="step-sub">from ${leg.from?.name ?? "the stop"} · ${s.detail}${
          leg.to?.name ? html` · get off at ${leg.to.name}` : ""
        }</span></span></li>`;
    })}`;
    return;
  }
  $("dir-steps-title").textContent = "Steps";
  $("dir-steps").innerHTML = html`${route.steps.map(
    (step) => html`<li class="step"><span>${step.instruction}</span>${
      step.distance_m > 0 ? html`<span class="step-sub">${formatDistance(step.distance_m)}</span>` : ""
    }</li>`
  )}`;
}

function selectRoute(index) {
  if (!dir.result || !dir.result.routes[index]) return;
  dir.selected = index;
  $("dir-cards").querySelectorAll(".route-card").forEach((card) => {
    card.setAttribute("aria-pressed", String(Number(card.dataset.index) === index));
  });
  renderSteps();
  drawRoutes();
}

/* ---- turn-by-turn

   Start follows the selected route live: the panel becomes the list of steps
   still ahead, the visitor is an arrow on the map turned by the phone's
   compass, and each GPS fix moves them along the route (navProgress,
   directions.js), dropping the steps behind them. Everything stays on the
   phone: fixes and headings never leave the page. */

const nav = {
  active: false,
  route: null,
  steps: [],
  // Each step's length along its own line, for the distance left overall.
  lengths: [],
  index: 0,
  // Distance left in the current step, null before the first fix.
  remaining: null,
  fix: null,
  watch: null,
  marker: null,
  // The compass heading, and the GPS course for when there is no compass.
  heading: null,
  course: null,
  orientEvent: null,
  onOrient: null,
  painting: false,
  follow: true,
  // Consecutive fixes off the route; a few in a row, not one stray fix.
  off: 0,
  rerouting: false,
  wake: null,
};
// Off the route: beyond this, or the fix's own accuracy if that is worse.
const NAV_OFF_ROUTE_M = 50;
const NAV_OFF_FIXES = 3;
// Close enough to read the next turn's street.
const NAV_ZOOM = 17;

function setNavNote(text) {
  $("nav-note").textContent = text;
}

/** The arrow: a dot with a point, turned on the map's own north so it keeps
    pointing the right way when the map is rotated. */
function userArrow() {
  const el = document.createElement("div");
  el.className = "user-arrow";
  el.setAttribute("aria-hidden", "true");
  el.append(document.createElement("span"));
  return new maplibregl.Marker({ element: el, rotationAlignment: "map", pitchAlignment: "map" });
}

/** Turn the arrow, at most once a frame -- a compass fires far faster. */
function paintArrow() {
  if (nav.painting || !nav.marker) return;
  nav.painting = true;
  requestAnimationFrame(() => {
    nav.painting = false;
    const heading = nav.heading ?? nav.course;
    nav.marker?.getElement().classList.toggle("has-heading", heading !== null);
    if (heading !== null) nav.marker?.setRotation(heading);
  });
}

/** Listen to the compass. iOS asks permission, and only from a tap -- Start
    is that tap -- so this runs before anything else awaits. */
async function startHeading() {
  if (nav.onOrient || !window.DeviceOrientationEvent) return;
  const DOE = window.DeviceOrientationEvent;
  try {
    if (typeof DOE.requestPermission === "function" && (await DOE.requestPermission()) !== "granted") {
      return;
    }
  } catch {
    return;
  }
  nav.onOrient = (event) => {
    const angle = screen.orientation?.angle ?? window.orientation ?? 0;
    const heading = headingFromOrientation(event, angle);
    if (heading === null) return;
    nav.heading = heading;
    paintArrow();
  };
  // The absolute event where the browser has one (Chrome on Android); iOS
  // puts webkitCompassHeading on the plain one.
  nav.orientEvent = "ondeviceorientationabsolute" in window ? "deviceorientationabsolute" : "deviceorientation";
  window.addEventListener(nav.orientEvent, nav.onOrient);
}

function stopHeading() {
  if (nav.onOrient) window.removeEventListener(nav.orientEvent, nav.onOrient);
  nav.onOrient = null;
  nav.heading = null;
}

/** Keep the screen on while navigating; the browser drops the lock when the
    page is hidden, so it is taken again when it comes back. */
async function holdScreenOn() {
  if (!nav.active || nav.wake || !navigator.wakeLock) return;
  try {
    nav.wake = await navigator.wakeLock.request("screen");
    nav.wake.addEventListener("release", () => { nav.wake = null; });
  } catch {
    nav.wake = null;
  }
}

/** Follow a route: its steps, from the start. */
function beginRoute(route) {
  nav.route = route;
  nav.steps = navSteps(route, dir.result?.timezone);
  nav.lengths = nav.steps.map((step) => projectOnLine(step.points, {
    lat: step.points[0]?.[1] ?? 0,
    lng: step.points[0]?.[0] ?? 0,
  })?.total ?? 0);
  nav.index = 0;
  nav.remaining = null;
  nav.off = 0;
  // A fix already in hand places the visitor on the new route at once.
  if (nav.fix) advanceNav(nav.fix, null);
}

async function startNavigation() {
  const route = dir.result?.routes[dir.selected];
  if (!route) return;
  if (!navigator.geolocation?.watchPosition) {
    setStatus("dir-status", "This browser cannot share its location, so live directions are unavailable.", true);
    return;
  }
  await startHeading();
  nav.active = true;
  nav.follow = true;
  nav.fix = null;
  beginRoute(route);
  $("dir-panel").classList.add("is-navigating");
  document.body.classList.add("navigating");
  $("dir-nav").hidden = false;
  $("nav-recenter").hidden = true;
  $("nav-steps-block").hidden = true;
  hideLocateNote();
  $("nav-steps-toggle").setAttribute("aria-expanded", "false");
  // The arrow is the visitor while navigating.
  if (userMarker) userMarker.getElement().hidden = true;
  closeDetail();
  nav.watch = navigator.geolocation.watchPosition(onNavFix, onNavError, {
    enableHighAccuracy: true,
    maximumAge: 0,
    timeout: 20000,
  });
  holdScreenOn();
  renderNav();
  // Start from the position already in hand while it is fresh, rather than
  // "Finding your location…" until the new watch's first fix.
  if (state.userLocation && Date.now() - (state.userFixAt ?? 0) < 30_000) {
    onNavFix({ coords: { latitude: state.userLocation.lat, longitude: state.userLocation.lng, accuracy: null, heading: null, speed: null } });
  }
}

function endNavigation() {
  if (!nav.active) return;
  nav.active = false;
  nav.rerouting = false;
  if (nav.watch !== null) navigator.geolocation.clearWatch(nav.watch);
  nav.watch = null;
  stopHeading();
  nav.marker?.remove();
  nav.marker = null;
  nav.wake?.release().catch(() => {});
  nav.wake = null;
  $("dir-panel").classList.remove("is-navigating");
  document.body.classList.remove("navigating");
  $("dir-nav").hidden = true;
  if (userMarker) userMarker.getElement().hidden = false;
}

/** Move the visitor along the route for fix `p`; `accuracy` in metres. */
function advanceNav(p, accuracy) {
  const progress = navProgress(nav.steps, p, nav.index);
  nav.index = progress.index;
  nav.remaining = progress.remaining;
  const tolerance = Math.max(NAV_OFF_ROUTE_M, accuracy ?? 0);
  nav.off = progress.offRoute !== null && progress.offRoute > tolerance ? nav.off + 1 : 0;
}

function onNavFix(position) {
  if (!nav.active) return;
  const { latitude, longitude, accuracy, heading, speed } = position.coords;
  const p = { lat: latitude, lng: longitude };
  nav.fix = p;
  // The compass, when there is one, says where the phone points; standing
  // still, GPS has no course at all.
  if (Number.isFinite(heading) && speed > 1) nav.course = heading;
  state.userLocation = p;

  if (!nav.marker) nav.marker = userArrow().setLngLat([longitude, latitude]).addTo(map);
  else nav.marker.setLngLat([longitude, latitude]);
  paintArrow();
  if (nav.follow) {
    // An offset rather than padding, as in flyAndSettle: clear of the panel.
    const pad = clearOfControls();
    map.easeTo({
      center: [longitude, latitude],
      zoom: Math.max(map.getZoom(), NAV_ZOOM),
      offset: [(pad.left - pad.right) / 2, (pad.top - pad.bottom) / 2],
      duration: 600,
    });
  }
  if (!nav.rerouting) advanceNav(p, accuracy);
  renderNav();
}

function onNavError(error) {
  if (!nav.active) return;
  setNavNote(
    error.code === error.PERMISSION_DENIED
      ? "Location is off for this site, so the directions cannot follow you. Allow it in the browser's settings."
      : "Waiting for your location…"
  );
}

/** Ask again from where the visitor is, and follow the new route. */
async function rerouteFromHere() {
  if (!nav.fix || nav.rerouting) return;
  nav.rerouting = true;
  renderNav();
  dir.fromSeq += 1;
  dir.from = { ...nav.fix, label: "Your location" };
  $("dir-from").value = dir.from.label;
  clearResult();
  placeMarkers();
  await submitDirections();
  nav.rerouting = false;
  if (!nav.active) return;
  // showResult has already switched to the new route if one came back.
  renderNav();
  if (!dir.result) setNavNote("No new route could be found from here.");
}

/** The panel while navigating: the next turn as a banner, the time and
    distance left, the route's own rating, and the steps still ahead. */
function renderNav() {
  if (!nav.active) return;
  const route = nav.route;
  const street = route.mode !== "transit";
  const arrived = nav.index >= nav.steps.length;
  const offRoute = !arrived && nav.off >= NAV_OFF_FIXES;
  const zone = dir.result?.timezone;

  setNavNote(
    nav.rerouting ? "Finding a new route from here…"
      : arrived ? "You have arrived."
      : offRoute ? "You are off the route."
      : nav.fix ? ""
      : "Finding your location…"
  );
  $("nav-reroute").hidden = !offRoute || nav.rerouting;

  // Distance left: the rest of this step, and every step after it.
  const rest = nav.lengths.slice(nav.index + 1).reduce((sum, m) => sum + m, 0);
  const left = arrived ? 0 : (nav.remaining ?? nav.lengths[nav.index] ?? 0) + rest;
  const speed = route.duration_s > 0 ? route.distance_m / route.duration_s : 0;
  const secondsLeft = speed > 0 ? left / speed : null;
  $("nav-left").textContent = arrived ? "Arrived" : secondsLeft === null ? formatDistance(left) : formatDuration(secondsLeft);
  const arrive = route.end_time
    ? clockIn(route.end_time, zone)
    : secondsLeft === null ? null : cityTimeLabel(zone, new Date(Date.now() + secondsLeft * 1000));
  $("nav-eta").textContent = arrived ? "" : `${formatDistance(left)}${arrive ? ` · you'll arrive at ${arrive}` : ""}`;

  // The route's own rating, as on its card.
  const activity = route.activity?.[state.track];
  const ratio = activity?.comparable ? activity.mean_ratio : null;
  const card = routeCardModel(dir.result, state.track).find((c) => c.index === dir.selected);
  const chip = $("nav-route-chip");
  chip.hidden = ratio === null;
  chip.dataset.band = band(ratio);
  chip.textContent = ratio === null ? "" : `${ROLE_WORDS[card?.role] ?? "This route"} · ${formatRatio(ratio)} the city average`;

  // A street step's instruction is done where it begins, so on step i the
  // next thing to do is step i + 1's, in what is left of step i. A transit
  // leg is the thing to do while it lasts.
  const current = nav.steps[nav.index];
  $("nav-on").textContent = !street || arrived || !current
    ? ""
    : nav.index === 0 ? current.text : current.street ? `On ${current.street}` : "";
  const ahead = arrived ? [] : nav.steps.slice(street ? nav.index + 1 : nav.index);
  const lead = nav.remaining === null ? ""
    : street ? `In ${formatDistance(nav.remaining)}`
    : current?.kind === "street" ? `${formatDistance(nav.remaining)} to go` : "";

  // The banner: the next thing to do.
  const next = ahead[0];
  $("nav-lead").textContent = arrived ? "" : lead || (next ? "Next" : "");
  $("nav-next").textContent = arrived ? "You have arrived" : next ? next.text : current?.text ?? "Follow the route";

  $("nav-steps").innerHTML = html`${ahead.map((step, i) => html`<li class="nav-step${i === 0 ? " is-next" : ""}">
    ${i === 0 && lead ? html`<span class="nav-lead">${lead}</span>` : ""}
    <span class="nav-text">${step.chip
      ? html`<span class="leg-chip" style="${step.chip.color ? `background:${step.chip.color};color:${step.chip.textColor}` : ""}">${step.chip.name}</span> `
      : ""}${step.text}</span>
    ${step.sub ? html`<span class="step-sub">${step.sub}</span>` : ""}
  </li>`)}`;
}

/* ---- the route on the map */

/** Route sources and layers, above the cells and roads but under the street
    and place names, so a name stays readable along the route. */
function addRouteLayers() {
  if (map.getSource("route-line")) return;
  const empty = { type: "FeatureCollection", features: [] };
  for (const id of ["route-alt", "route-line", "route-activity"]) {
    map.addSource(id, { type: "geojson", data: empty });
  }
  // The first label above the last road: the basemap puts some water labels
  // before its roads, and a route under the roads is hidden by them.
  const layers = map.getStyle().layers;
  const lastRoad = layers.findLastIndex((layer) => /^(tunnel|road|bridge|highway)/.test(layer.id));
  const before = layers.slice(lastRoad + 1).find((layer) => layer.type === "symbol")?.id;
  // The other routes: dotted, quiet, still clickable.
  map.addLayer({
    id: "route-alt-line", type: "line", source: "route-alt",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": themeColor("--route-alt", "#8a766b"), "line-width": 5, "line-opacity": 0.75, "line-dasharray": [0.3, 1.6] },
  }, before);
  // The same lines, wide and invisible, as what a tap or click hits: a
  // dotted 5 px line is too thin to pick with a finger. Opacity 0, not
  // hidden -- MapLibre does not hit-test a layer that is not drawn.
  map.addLayer({
    id: "route-alt-hit", type: "line", source: "route-alt",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#000000", "line-width": 28, "line-opacity": 0 },
  }, before);
  // A casing: the route sits on cells painted in the same ramp it is
  // coloured with, and only an edge keeps a green route off a green cell.
  map.addLayer({
    id: "route-casing", type: "line", source: "route-line",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": themeColor("--route-casing", "#ffffff"), "line-width": 12, "line-opacity": 0.95 },
  }, before);
  map.addLayer({
    id: "route-line", type: "line", source: "route-line",
    filter: ["!", ["get", "walk"]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["coalesce", ["get", "color"], primaryColor()], "line-width": 7 },
  }, before);
  map.addLayer({
    id: "route-walk", type: "line", source: "route-line",
    filter: ["get", "walk"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": primaryColor(), "line-width": 7, "line-dasharray": [0.1, 1.8] },
  }, before);
  // The reported activity along the route, stretch by stretch, on the map's
  // own ramp (route.activity[track].segments from /directions), drawn over the
  // plain line. Only scored stretches have segments, so a transit ride keeps
  // its line colour between the walks.
  map.addLayer({
    id: "route-activity", type: "line", source: "route-activity",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": [
        "case", ["<", ["get", "ratio"], 0], zeroFill(),
        ["interpolate", ["linear"], ["get", "ratio"], ...valueRampStops(VALUE_RAMP)],
      ],
      "line-width": 7,
    },
  }, before);
}

function wireRouteLayerEvents() {
  map.on("click", "route-alt-hit", (event) => {
    const alt = event.features?.[0];
    if (alt && dir.open) selectRoute(alt.properties.index);
  });
  map.on("mouseenter", "route-alt-hit", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "route-alt-hit", () => {
    map.getCanvas().style.cursor = "";
  });
}

function drawRoutes() {
  const result = dir.result;
  if (!result || !dir.open) return;
  const route = result.routes[dir.selected];
  map.getSource("route-alt").setData(altFeatures(result.routes, dir.selected));
  map.getSource("route-line").setData(routeLineFeatures(route));
  map.getSource("route-activity").setData(segmentFeatures(route.activity?.[state.track]));
  placeMarkers();
}

function clearRouteLayers({ keepMarkers = false } = {}) {
  const empty = { type: "FeatureCollection", features: [] };
  for (const id of ["route-alt", "route-line", "route-activity"]) map.getSource(id)?.setData(empty);
  if (keepMarkers) return;
  for (const end of ["from", "to"]) {
    dir.markers[end]?.remove();
    dir.markers[end] = null;
  }
}

/* ---- wiring */

function wireSearch() {
  wireCombo($("search-input"), $("search-list"), {
    statusId: "search-status",
    onChoose: showPlace,
  });
  $("search-clear").onclick = () => {
    resetSearch();
    $("search-input").focus();
  };
  $("btn-locate").onclick = locateMe;
  $("dir-start").onclick = startNavigation;
  $("dir-swap").onclick = swapEnds;
  // Steps, under the cards; all steps while navigating.
  for (const [toggle, block] of [["dir-steps-toggle", "dir-steps-block"], ["nav-steps-toggle", "nav-steps-block"]]) {
    $(toggle).onclick = () => {
      const open = $(block).hidden;
      $(block).hidden = !open;
      $(toggle).setAttribute("aria-expanded", String(open));
    };
  }
  $("nav-end").onclick = () => {
    endNavigation();
    $("dir-start").focus();
  };
  $("nav-reroute").onclick = rerouteFromHere;
  $("nav-recenter").onclick = () => {
    nav.follow = true;
    $("nav-recenter").hidden = true;
    if (nav.fix) map.easeTo({ center: [nav.fix.lng, nav.fix.lat], zoom: Math.max(map.getZoom(), NAV_ZOOM) });
  };
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) holdScreenOn();
  });
  $("dir-close").onclick = () => {
    closeDirections();
    document.querySelector('.rail-btn[data-view="directions"]')?.focus();
  };

  wireCombo($("dir-to"), $("dir-to-list"), {
    statusId: "dir-status",
    onChoose: (place) => (latLngFromString(place.label) ? setToPoint(place) : setTo(place)),
    // Typing over a chosen destination un-chooses it.
    onType: () => {
      if (!search.place) return;
      search.place = null;
      $("dir-to").closest(".dir-field").classList.remove("is-set");
      clearResult();
      placeMarkers();
    },
  });

  wireCombo($("dir-from"), $("dir-from-list"), {
    statusId: "dir-status",
    // A typed lat, lng is named and checked like a located point; a place
    // from the search is already known to be in the city.
    onChoose: (place) => (latLngFromString(place.label) ? setFromPoint(place) : setFrom(place)),
    // Typing over a chosen start un-chooses it.
    onType: () => {
      if (!dir.from) return;
      dir.fromSeq += 1;
      dir.from = null;
      clearResult();
      placeMarkers();
    },
  });

  $("dir-from-here").onclick = async () => {
    const button = $("dir-from-here");
    button.disabled = true;
    setStatus("dir-status", "Locating…");
    try {
      const point = await currentPosition();
      state.userLocation = point;
      showUserDot(point);
      followLocation();
      await setFromPoint(point);
    } catch (error) {
      setStatus("dir-status", `Could not read your location: ${error.message}`, true);
    } finally {
      button.disabled = false;
    }
  };

  $("dir-modes").querySelectorAll("[data-mode]").forEach((tab) => {
    tab.onclick = () => {
      if (tab.disabled || dir.mode === tab.dataset.mode) return;
      dir.mode = tab.dataset.mode;
      syncModeTabs();
      clearResult();
      submitDirections();
    };
  });
  // Arrow keys move between the mode tabs, as in any tablist.
  $("dir-modes").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    const tabs = [...$("dir-modes").querySelectorAll("[data-mode]:not(:disabled)")];
    const at = tabs.indexOf(document.activeElement);
    if (at < 0) return;
    const next = tabs[(at + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    next.focus();
    next.click();
  });

  $("dir-cards").addEventListener("click", (event) => {
    const card = event.target.closest(".route-card");
    if (card) selectRoute(Number(card.dataset.index));
  });
}

/* ----------------------------------------------------------------------- map */

/**
 * The basemap layer the cells are slotted in under: the first road, rail or
 * label layer. Land, parks and water stay beneath the colour; streets and the
 * neighbourhood and street names draw over it, so the map stays readable
 * through the ramp. The raster fallback is one opaque image with nothing to
 * slot under, so there the cells go on top.
 */
function cellsBeforeId() {
  const above = map.getStyle().layers.find(
    (layer) => layer.type === "symbol" || /^(tunnel|road|bridge|rail)/.test(layer.id)
  );
  return above?.id;
}

/**
 * The cells, and the outline of the selected one, under the basemap's roads
 * and labels (cellsBeforeId). Safe to call again after a style change, which
 * drops every source and layer the page added.
 */
function addCellLayers() {
  if (!map.getSource("cells")) {
    map.addSource("cells", {
      type: "geojson",
      data: { type: "FeatureCollection", features: state.features },
      promoteId: "h3",
    });
  }
  const before = cellsBeforeId();
  if (!map.getLayer("cells-fill")) {
    map.addLayer({
      id: "cells-fill",
      type: "fill",
      source: "cells",
      paint: { "fill-color": fillColorExpression(), "fill-opacity": cellsOpacity() },
    }, before);
  }
  // The selected hexagon, outlined in the page's orange. Still a hexagon:
  // only the interface around the map is rounded.
  if (!map.getLayer("cells-outline")) {
    map.addLayer({
      id: "cells-outline",
      type: "line",
      source: "cells",
      layout: { "line-join": "round" },
      paint: {
        "line-color": primaryColor(),
        "line-width": 3.5,
        "line-opacity": ["case", ["boolean", ["feature-state", "selected"], false], 1, 0],
      },
    }, before);
  }

  // Building footprints are fills the basemap draws after its roads. Left above
  // the cells they would grey the colour out block by block at street zoom, so
  // they go underneath with the land and water.
  const layers = map.getStyle().layers;
  const cellsAt = layers.findIndex((layer) => layer.id === "cells-fill");
  for (const layer of layers.slice(cellsAt + 1)) {
    if (layer.type === "fill") map.moveLayer(layer.id, "cells-fill");
  }
}

/** Every layer the page draws on the basemap. */
function addOverlayLayers() {
  addCellLayers();
  addRouteLayers();
  addNearLayers();
  addCityLayers();
}

/* The moment of the last press-and-hold, so the tap the finger's lift
   produces does not also close or pick a cell. */
let longPressAt = 0;
const justLongPressed = () => Date.now() - longPressAt < 800;

/**
 * Right-click on desktop, press-and-hold on a phone: the same action
 * (pinClickedPoint). iOS Safari never sends contextmenu for a touch, so the
 * hold is timed here: 500 ms on one finger that moves less than 10 px. Android
 * Chrome sends both; the second is dropped.
 */
function wireContextPress() {
  const press = (lngLat) => {
    // Only while address search is on: the box it fills is hidden otherwise.
    // Not while navigating: a new pin would end the trip being followed.
    if ($("search-field").hidden || nav.active) return;
    pinClickedPoint(lngLat);
  };
  map.on("contextmenu", (event) => {
    if (justLongPressed()) return;
    press(event.lngLat);
  });

  const surface = map.getCanvasContainer();
  let timer = null;
  let start = null;
  const cancel = () => {
    clearTimeout(timer);
    timer = null;
  };
  surface.addEventListener("touchstart", (event) => {
    cancel();
    if (event.touches.length !== 1) return;
    const touch = event.touches[0];
    start = { x: touch.clientX, y: touch.clientY };
    timer = setTimeout(() => {
      timer = null;
      longPressAt = Date.now();
      navigator.vibrate?.(12);
      const rect = surface.getBoundingClientRect();
      press(map.unproject([start.x - rect.left, start.y - rect.top]));
    }, 500);
  }, { passive: true });
  surface.addEventListener("touchmove", (event) => {
    if (!timer) return;
    const touch = event.touches[0];
    if (Math.hypot(touch.clientX - start.x, touch.clientY - start.y) > 10) cancel();
  }, { passive: true });
  surface.addEventListener("touchend", cancel);
  surface.addEventListener("touchcancel", cancel);
}

/* The theme the basemap on screen was drawn for, and whether initMap has
   finished (a theme switch before then is picked up at its end). */
let mapTheme = null;
let mapReady = false;

async function initMap() {
  mapTheme = currentTheme();
  const style = await resolveStyle(mapTheme);
  map = new maplibregl.Map({
    container: "map",
    style,
    // The whole country until a city is served: frameCity then fits the first
    // city's own bounds, at duration 0.
    center: [-98.5, 39.5],
    zoom: 3,
    // No minZoom. With six cities spread across the country, a floor tight
    // enough for one city is a floor that cannot show another -- and fitBounds
    // on Los Angeles needs to go wider than a Philadelphia-shaped limit allows.
    maxZoom: 17,
    attributionControl: { compact: true },
  });

  // Zoom is the page's own round buttons (#zoom-in, #zoom-out); the scale bar
  // stays MapLibre's.
  map.addControl(new maplibregl.ScaleControl({ maxWidth: SCALE_MAX_WIDTH, unit: "imperial" }), "bottom-right");

  await new Promise((resolve) => map.on("load", resolve));
  addOverlayLayers();

  map.on("mousemove", "cells-fill", (event) => {
    const feature = event.features?.[0];
    if (!feature) return;
    map.getCanvas().style.cursor = "pointer";

    if (state.hovered && state.hovered !== feature.id) {
      map.setFeatureState({ source: "cells", id: state.hovered }, { hover: false });
    }
    state.hovered = feature.id;
    map.setFeatureState({ source: "cells", id: feature.id }, { hover: true });
  });

  map.on("mouseleave", "cells-fill", () => {
    map.getCanvas().style.cursor = "";
    if (state.hovered) {
      map.setFeatureState({ source: "cells", id: state.hovered }, { hover: false });
      state.hovered = null;
    }
  });

  map.on("click", "cells-fill", (event) => {
    if (justLongPressed()) return;
    // A click on another route picks that route, not the cell under it.
    if (dir.open && map.getLayer("route-alt-hit") && map.queryRenderedFeatures(event.point, { layers: ["route-alt-hit"] }).length) return;
    const feature = event.features?.[0];
    if (feature) selectCell(feature.properties.h3);
  });
  map.on("click", (event) => {
    if (justLongPressed()) return;
    // Mid theme switch the page's layers are briefly not on the map.
    if (!map.getLayer("cells-fill")) return;
    const onCell = map.queryRenderedFeatures(event.point, { layers: ["cells-fill"] }).length;
    if (!onCell) closeDetail();
    setKeyOpen(false);
  });
  wireContextPress();
  wireRouteLayerEvents();
  wireCityLayerEvents();
  // Moving the map by hand stops it following the visitor, until Recenter.
  map.on("dragstart", (event) => {
    if (!nav.active || !event.originalEvent) return;
    nav.follow = false;
    $("nav-recenter").hidden = false;
  });

  // After every move: which city the page is in, the cell size the zoom calls
  // for (and any city that came into view at it), and the compass. Kept, so a
  // search can wait for the layer its own move set loading. Registered before
  // flyAndSettle's own moveend listener, so it always runs first.
  map.on("moveend", () => {
    updateFocus();
    resSync = syncResToZoom();
    renderCompass();
  });
  // The roses turn against the map, so their N stays on real north.
  map.on("rotate", turnRoses);

  window.__safetyMap = map;
  mapReady = true;
  // The theme may have changed while the map was being made (a system
  // switch, or a tap on the theme button during the first load).
  if (mapTheme !== currentTheme()) applyMapTheme();
}

/**
 * Swap the basemap for the theme (Carto's Positron or Dark Matter, from the
 * hosts the CSP already allows) and put every page layer back on it with the
 * data already in hand. setStyle drops sources, layers and feature state.
 */
let themeSeq = 0;
async function applyMapTheme() {
  if (!mapReady) return;
  const seq = ++themeSeq;
  const theme = currentTheme();
  const style = await resolveStyle(theme);
  if (seq !== themeSeq) return;
  mapTheme = theme;
  map.setStyle(style, { diff: false });
  map.once("style.load", () => {
    if (seq !== themeSeq) return;
    state.hovered = null;
    addOverlayLayers();
    if (state.selected) map.setFeatureState({ source: "cells", id: state.selected }, { selected: true });
    renderCityMarkers();
    drawRoutes();
    renderNearOverlay();
    renderLegend();
  });
  renderLegend();
}

/* --------------------------------------------------------------------- wiring */

/** Recolour from data already in hand -- no refetch. */
function repaint() {
  map.setPaintProperty("cells-fill", "fill-color", fillColorExpression());
  renderCompass();
}

/** The cells' opacity: brighter where hovered or selected -- or nothing at
    all with Show tiles off. Zero, not a hidden layer: MapLibre does not
    hit-test a layer with visibility "none", and the popup needs the click. */
function cellsOpacity() {
  if (!state.tiles) return 0;
  // UI 5: about 0.5 in light and 0.46 in dark, so street names stay readable.
  const base = Number(themeColor("--cell-opacity", "0.5")) || 0.5;
  return [
    "case",
    ["boolean", ["feature-state", "selected"], false], Math.min(1, base + 0.3),
    ["boolean", ["feature-state", "hover"], false], Math.min(1, base + 0.2),
    base,
  ];
}

/** The track toggle picks the colour the cells are painted in, so it shows
    only while the tiles are on. */
function setTiles(on) {
  state.tiles = on;
  $("f-tiles").setAttribute("aria-checked", String(on));
  map.setPaintProperty("cells-fill", "fill-opacity", cellsOpacity());
}

/** Which offences the colour ranks. Every .track-seg on the page is this one
    choice, so they all move together. Both tracks are rated on every feature
    (rateCity): switching is a repaint, with no request and no loading state. */
function setTrack(track) {
  if (!TRACK_PROPS[track]) return;
  state.track = track;
  document.querySelectorAll(".track-seg [data-track]").forEach((b) => {
    b.setAttribute("aria-selected", String(b.dataset.track === track));
  });
  repaint();
  // The card's rating and charts are per track too, and the detail is
  // already in hand -- re-read it rather than re-request it.
  if (state.detail && state.selected) showCellPopup(state.selected, state.detail);
  // Both tracks ride on every route too.
  if (dir.result) renderDirections();
}

/* ---- the shell: sections, theme, zoom, the key, help */

const VIEWS = ["explore", "directions", "near", "cities"];

/**
 * Open one section. Explore is the map; Directions, Near me and Cities each
 * have a panel (left on desktop, a sheet or screen on a phone). Help is a
 * dialog over whichever is open.
 */
function setView(view, { focus = false } = {}) {
  if (!VIEWS.includes(view)) return;
  const before = document.body.dataset.view;
  if (view !== "directions" && before === "directions" && dir.open) closeDirections({ toView: false });
  // The buddy's map tip belongs to the section it was said in.
  if (view !== before) hideLocateNote();
  document.body.dataset.view = view;
  document.querySelectorAll(".rail-btn[data-view]").forEach((b) => {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  $("near-panel").hidden = view !== "near";
  $("cities-panel").hidden = view !== "cities";
  setKeyOpen(false);
  document.body.classList.remove("near-on-map");
  $("near-show-map").setAttribute("aria-pressed", "false");
  $("near-show-map").textContent = "Show on map";
  if (view === "directions" && !dir.open) openDirections({ focus });
  if (view === "near") renderCompass();
  if (view === "cities") renderCities();
  renderNearOverlay();
  // From the rail, focus follows into the section just opened.
  const heading = focus ? $({ near: "near-title", cities: "cities-title" }[view] ?? "") : null;
  if (heading) {
    heading.setAttribute("tabindex", "-1");
    heading.focus();
  }
}

/** The phone's map key (the Explore card as a sheet). */
function setKeyOpen(open) {
  document.body.classList.toggle("key-open", open);
  $("btn-key").setAttribute("aria-expanded", String(open));
}

/** The sun / moon buttons show the theme on screen; the phone's single map
    button offers the other one. */
function syncThemeButtons() {
  const theme = currentTheme();
  document.querySelectorAll("[data-theme-choice]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.themeChoice === theme));
  });
  const other = theme === "dark" ? "light" : "dark";
  $("theme-fab").setAttribute("aria-label", `Switch to ${other} mode`);
  $("theme-fab").querySelector("use").setAttribute("href", other === "dark" ? "#i-moon" : "#i-sun");
}

/** The legend, built from RAMP_HINGES: one row per band, its range read off
    the hinges, its swatch the ramp colour at the middle of it. */
function renderLegend() {
  const [a, b, c] = BAND_EDGES;
  const x = (n) => `${n}×`;
  const rows = [
    { band: "calm", range: `under ${x(a)}`, sw: `linear-gradient(135deg, ${colorForRatio(0)} 0 50%, ${colorForRatio(a * 0.5)} 50%)` },
    { band: "busier", range: `${x(a)} to ${x(b)}`, sw: colorForRatio(a) },
    { band: "busy", range: `${x(b)} to ${x(c)}`, sw: colorForRatio(b) },
    { band: "very", range: `${x(c)} and up`, sw: colorForRatio(c) },
    { band: "none", range: "no reports", sw: zeroFill() },
  ];
  const items = html`${rows.map((r) => html`<li><span class="sw" style="--sw:${r.sw}" aria-hidden="true"></span>${bandName(r.band)}<span class="range">${r.range}</span></li>`)}`;
  $("legend").innerHTML = items;
  $("legend-strip").innerHTML = items;
}

/* ---- Help: "How do we figure this out?" */

let helpOpener = null;

function openHelp(opener = null) {
  const dialog = $("help");
  if (dialog.open) return;
  helpOpener = opener;
  document.querySelectorAll(".rail-btn[data-open-help]").forEach((b) => b.setAttribute("aria-expanded", "true"));
  dialog.showModal();
}

function closeHelp() {
  $("help").close();
}

/** The full method, as the server writes it for the city on screen. Every
    field is server prose (registry text included) and goes through html``;
    terms_url becomes a link only if it is a plain http(s) URL. Fields the
    server leaves out are skipped rather than shown empty. */
async function loadMethodology() {
  const body = $("methodology-body");
  if (body.dataset.city === state.city) return;
  body.dataset.city = state.city ?? "";
  body.textContent = "Loading…";
  let m;
  try {
    const response = await fetch(`${API}/methodology?city=${encodeURIComponent(state.city ?? DEFAULT_CITY)}`);
    if (!response.ok) throw new Error(String(response.status));
    m = await response.json();
  } catch {
    body.textContent = "The full method could not be loaded just now. Try again shortly.";
    delete body.dataset.city;
    return;
  }
  const list = (items) => (Array.isArray(items) && items.length ? html`<ul>${items.map((line) => html`<li>${line}</li>`)}</ul>` : "");
  const para = (text) => (text ? html`<p>${text}</p>` : "");
  const row = (label, value) => (value === null || value === undefined || value === "" ? "" : html`<div><dt>${label}</dt><dd>${value}</dd></div>`);
  const pct = (share) => (typeof share === "number" ? `${(share * 100).toFixed(1)}%` : null);
  const terms = safeHttpUrl(m.terms_url);
  const cell = m.cell_model ?? {};
  const safety = m.safety_measure;
  const tod = m.time_of_day;
  body.innerHTML = html`
    ${para(m.what_this_shows)}
    ${m.what_this_is_not?.length ? html`<div class="callout"><h3>What this is not</h3>${list(m.what_this_is_not)}</div>` : ""}
    ${m.known_limitations?.length ? html`<h3>Known limitations</h3>${list(m.known_limitations)}` : ""}
    <h3>The hexagons</h3>
    ${cell.grid ? html`<p>${cell.grid} hexagons at resolution ${cell.primary_resolution ?? "—"}${
      cell.primary_resolution_note ? ` (${cell.primary_resolution_note})` : ""}${
      cell.detail_resolution ? html`, and resolution ${cell.detail_resolution}${
        cell.detail_resolution_note ? ` (${cell.detail_resolution_note})` : ""} when you zoom in` : ""}.</p>` : ""}
    ${para(cell.window)}
    ${para(cell.relative_measure)}
    ${safety ? html`<h3>The ranking</h3>${para(safety.what_it_is)}${para(safety.why_two_rankings)}
      ${para(safety.weights?.note)}
      <dl>${row("Severity source", safety.weights?.source)}${row("Weight scheme", safety.weights?.scheme_version)}${
        row("Published weights cover", pct(safety.weights?.published_share) && `${pct(safety.weights.published_share)} of incidents`)}</dl>
      ${para(safety.weights?.fallback_note)}${para(safety.smoothing?.note)}${list(safety.known_limitations)}` : ""}
    ${tod ? html`<h3>Time of day</h3>${para(tod.what_it_is)}${list(tod.how_it_works)}${para(tod.hour_factor_note)}
      ${tod.timestamp_caveat ? html`<div class="callout"><h3>What the timestamps are</h3><p>${tod.timestamp_caveat}</p></div>` : ""}
      <dl>${row("Incidents with a known hour", pct(tod.coverage?.hour_known_share))}</dl>
      ${para(tod.coverage?.note)}${para(tod.scope?.note)}${list(tod.known_limitations)}` : ""}
    ${m.classification ? html`<h3>Offence classification</h3>${para(m.classification.standard)}
      <dl>${row("Crosswalk version", m.classification.crosswalk_version)}</dl>` : ""}
    <h3>Source and attribution</h3>
    ${para(m.attribution)}
    <dl>${row("Data as of", m.data_as_of ? new Date(m.data_as_of).toLocaleString() : null)}${
      row("Coverage", m.coverage?.start && m.coverage?.end ? `${m.coverage.start} to ${m.coverage.end}` : null)}${
      row("Incidents loaded", typeof m.coverage?.incidents === "number" ? nf.format(m.coverage.incidents) : null)}${
      row("Update cadence", m.update_cadence)}</dl>
    ${para(m.freshness_note)}${para(m.location_precision_note)}
    ${terms ? html`<p><a href="${terms}" target="_blank" rel="noopener">Source dataset and terms of use</a></p>` : ""}
  `;
}

function wireHelp() {
  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-open-help]");
    if (opener) openHelp(opener);
  });
  document.querySelectorAll("[data-close-help]").forEach((b) => { b.onclick = closeHelp; });
  const dialog = $("help");
  // A click on the backdrop (the dialog itself, outside its content) closes it.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeHelp();
  });
  dialog.addEventListener("close", () => {
    document.querySelectorAll(".rail-btn[data-open-help]").forEach((b) => b.removeAttribute("aria-expanded"));
    helpOpener?.focus?.();
    helpOpener = null;
  });
  $("help-full").addEventListener("toggle", () => {
    if ($("help-full").open) loadMethodology();
  });
  // The second step's picture: a little field of hexagons on the ramp.
  const tones = [0, 0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.2, 2.6, 3.2];
  $("help-hexes").innerHTML = html`${Array.from({ length: 40 }, (_, i) => {
    const t = tones[(i * 7 + Math.floor(i / 10) * 3) % tones.length];
    return html`<i style="background:${colorForRatio(t)}"></i>`;
  })}`;
}

function wireShell() {
  // Any button marked with a section opens it: the rail, the city chip, the
  // small compass, the load pill. Buttons only: <body> carries data-view too
  // (the open section, for the stylesheet), and matching it would re-run
  // setView on every click anywhere -- undoing "Show on map" and closing the
  // phone's map key the moment it opened.
  document.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-view]");
    if (target && !target.closest("#city-grid")) setView(target.dataset.view, { focus: target.classList.contains("rail-btn") });
  });
  document.querySelectorAll("[data-theme-choice]").forEach((b) => {
    b.onclick = () => setTheme(b.dataset.themeChoice);
  });
  $("theme-fab").onclick = () => setTheme(currentTheme() === "dark" ? "light" : "dark");
  document.addEventListener("themechange", () => {
    syncThemeButtons();
    applyMapTheme();
  });
  syncThemeButtons();

  $("zoom-in").onclick = () => map.zoomIn();
  $("zoom-out").onclick = () => map.zoomOut();
  $("btn-key").onclick = () => setKeyOpen(!document.body.classList.contains("key-open"));
  $("see-close").onclick = () => {
    setKeyOpen(false);
    $("btn-key").focus();
  };

  $("f-tiles").onclick = () => setTiles(!state.tiles);
  document.querySelectorAll(".track-seg").forEach((seg) => {
    seg.addEventListener("click", (event) => {
      const button = event.target.closest("[data-track]");
      if (button) setTrack(button.dataset.track);
    });
    // Arrow keys move along the toggle, as in any tablist.
    seg.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const tabs = [...seg.querySelectorAll("[data-track]")];
      const next = tabs[(tabs.indexOf(document.activeElement) + 1) % tabs.length];
      next.focus();
      setTrack(next.dataset.track);
    });
  });

  // Only moves the map; updateFocus keeps the chips on the city it lands in.
  const pickCity = (event) => {
    const button = event.target.closest("[data-city]:not(:disabled)");
    if (!button) return;
    if (isNarrow()) setView("explore");
    switchCity(button.dataset.city);
  };
  // Near me on a phone: drop the screen to a short card, so the compass
  // drawn on the map shows; again to bring it back.
  $("near-show-map").onclick = () => {
    const on = document.body.classList.toggle("near-on-map");
    $("near-show-map").setAttribute("aria-pressed", String(on));
    $("near-show-map").textContent = on ? "Back to the compass" : "Show on map";
    const origin = state.compass?.origin;
    if (on && origin) map.easeTo({ center: [origin.lng, origin.lat], zoom: Math.max(map.getZoom(), 13.5), duration: 600 });
    renderNearOverlay();
  };
  $("city-grid").addEventListener("click", pickCity);

  wireHelp();
  narrowMedia.addEventListener?.("change", () => {
    setKeyOpen(false);
    renderNearOverlay();
  });
  // Escape closes the topmost floating thing.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    if (document.body.classList.contains("key-open")) setKeyOpen(false);
    else if (!$("locate-note").hidden) hideLocateNote();
    else if (!$("cell-card").hidden) closeDetail();
  });
}

function wireControls() {
  wireShell();
  wireSearch();
  $("locate-note-close").onclick = hideLocateNote;
}

/* Poll the pipeline's own refresh stamp; reload every city's layer when the
   ETL has run. While no load is being shown, also look in on the progress
   endpoint, so a load a later deploy starts gets its bar without a reload. */
function watchForRefresh() {
  // Skipped while the tab is hidden: a background tab has nobody to show a
  // fresher layer to. Coming back runs a tick at once, so it catches up.
  const tick = async () => {
    if (document.hidden) return;
    if (progressTimer === null) pollProgress();
    try {
      const response = await fetch(`${API}/version`);
      if (!response.ok) return;
      const version = await response.json();
      const stamp = `${version.last_refreshed_at}|${version.cities ?? ""}`;
      if (state.refreshStamp && stamp !== state.refreshStamp) {
        await onCitiesChanged();
        await refreshLayers({ force: true, quiet: true });
      }
      state.refreshStamp = stamp;
    } catch {
      /* transient; try again next tick */
    }
  };
  setInterval(tick, 60_000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick();
  });
}

/** Frame the first city to open on and load every city's layer. The city is
    the one the visitor is standing in, where they have already granted their
    location, else DEFAULT_CITY, else the first served. */
async function openOnFirstCity() {
  const here = await cityFromGrantedLocation();
  const first = cityById(here) ?? cityById(DEFAULT_CITY) ?? state.cities[0];
  frameCity(first);
  updateFocus();
  state.res = resForZoom();
  await refreshLayers();
}

(async function main() {
  await initMap();
  wireControls();
  wireCellCard();
  renderLegend();
  renderTimeChips();
  // Not awaited: the map does not wait on routing being up, nor on the bar.
  loadDirectionsStatus();
  pollProgress();
  // Expose read-only state for debugging and for the smoke-test driver.
  window.__safetyState = state;

  try {
    await loadCities();
  } catch (error) {
    console.error(error);
    showLoading("Could not reach the map's data. Try again shortly.", true);
  }
  if (state.cities.length) await openOnFirstCity();
  // With no city served yet, the progress bar is the page; pollProgress opens
  // the map on the first city to finish (onCitiesChanged).

  const version = await fetch(`${API}/version`).then((r) => r.json()).catch(() => null);
  state.refreshStamp = version ? `${version.last_refreshed_at}|${version.cities ?? ""}` : null;
  watchForRefresh();
  watchCityClock();
})();
