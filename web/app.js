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

/* A distinct state, not the bottom of the ramp: nothing was reported here. */
const ZERO_FILL = "#e1e0d9";
const SERIES_1 = "#2a78d6";
const INK_SECONDARY = "#52514e";
const BASELINE = "#c3c2b7";

/* Safety ramp, least safe -> safest, applied continuously to the percentile.

   Built in OKLCH, running red -> orange -> yellow -> green with chroma held as
   high as sRGB allows at every step, so the whole ramp stays vivid. An earlier
   version pinched chroma to nothing at the midpoint, and the two arms met in a
   dull grey that read as "no data" rather than "middling"; passing through
   yellow keeps the centre as saturated as the ends.

   Lightness rises from red to a peak at yellow and settles back for green, so
   the safest end is not the lightest. Red-green is still separated by
   lightness (dark red against mid green), and the table view carries the exact
   percentile for anyone who cannot rely on hue.

   Out-of-gamut steps are mapped by reducing chroma rather than letting
   channels clamp, so no two steps collapse onto the same hex. All twenty are
   distinct. */
const SAFETY = [
  "#db0118", "#e32503", "#e74001", "#ea5403", "#ef6403",
  "#f47204", "#f68a07", "#f9a000", "#fab60a", "#f9cc06",
  "#f0d80a", "#dfd80d", "#cdd904", "#b9da04", "#a2dc02",
  "#8bd802", "#73d216", "#5acc25", "#39c631", "#09bf3f",
];

/* The incident count rides the same ramp, reversed: both views then agree that
   red is the concerning end, so switching between them is a change of measure
   and not a change of vocabulary. Quiet cells land in the green. */
const COUNT_RAMP = [...SAFETY].reverse();

/** Midpoint of two hexes in sRGB. Adjacent ramp steps are ~0.025 apart in
    lightness, so averaging channels here is indistinguishable from doing it in
    OKLCH, and it avoids carrying a colour-space conversion for one blend. */
function mixHex(a, b) {
  const parse = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const [ar, ag, ab] = parse(a);
  const [br, bg, bb] = parse(b);
  const channel = (x, y) => Math.round((x + y) / 2).toString(16).padStart(2, "0");
  return `#${channel(ar, br)}${channel(ag, bg)}${channel(ab, bb)}`;
}

/**
 * The ramp with an explicit centre step inserted.
 *
 * Twenty steps have no middle one: they sit at i/19, and 0.5 falls between the
 * tenth and eleventh. The value scale puts 150% of the city average on the centre step,
 * so without a stop there the average lands inside a segment and its colour is
 * a blend rather than the yellow it is meant to be.
 */
function withMidpoint(colors) {
  const half = colors.length / 2;
  return [
    ...colors.slice(0, half),
    mixHex(colors[half - 1], colors[half]),
    ...colors.slice(half),
  ];
}

/* Twenty-one steps: the ramp plus the centre the yellow point sits on. */
const VALUE_RAMP = withMidpoint(COUNT_RAMP);

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

/** The ratio in whichever form reads as a quantity: 1,400% has to be decoded,
    14.0x does not.

    The bottom end is named rather than rounded, rather than
    shown as zero: a big quiet cell -- one report among twenty thousand
    people -- rounds to 0%, and 0% is the one thing this figure must not say
    about a cell where something was reported. */
function formatRatio(ratio) {
  if (ratio >= 10) return `${ratio.toFixed(1)}×`;
  if (ratio < 0.005) return "<1%";
  return `${Math.round(ratio * 100)}%`;
}

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

const CARTO_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";

// Used only if the vector style is unreachable, so the page still renders a map.
const RASTER_FALLBACK = {
  version: 8,
  sources: {
    carto: {
      type: "raster",
      tiles: [
        "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
    },
  },
  layers: [{ id: "carto", type: "raster", source: "carto" }],
};

async function resolveStyle() {
  try {
    const response = await fetch(CARTO_STYLE);
    if (response.ok) return await response.json();
  } catch {
    /* fall through */
  }
  return RASTER_FALLBACK;
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
    ["<", ratio, 0], ZERO_FILL,
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

/** Eight 45° wedges, each tinted with its sector's mean on the map's ramp. */
function renderCompassWedges(sectors) {
  const wedge = (i) => {
    const a0 = ((i * 45 - 22.5 - 90) * Math.PI) / 180;
    const a1 = ((i * 45 + 22.5 - 90) * Math.PI) / 180;
    const r = 49;
    return `M0 0 L${(r * Math.cos(a0)).toFixed(2)} ${(r * Math.sin(a0)).toFixed(2)} ` +
      `A${r} ${r} 0 0 1 ${(r * Math.cos(a1)).toFixed(2)} ${(r * Math.sin(a1)).toFixed(2)} Z`;
  };
  $("compass-wedges").innerHTML = html`${(sectors ?? []).map(
    (s, i) => html`<path d="${wedge(i)}" fill="${
      s.mean === null ? "none" : rampColorAt(s.mean)
    }" fill-opacity="0.55"></path>`
  )}<circle r="20" fill="var(--surface-1)"></circle>`;
}

/** Point the needle at the quietest nearby sector, or say there isn't one. */
function renderCompass() {
  const box = $("compass");
  if (!map || !state.featureIndex?.size) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  // The rose turns against the map, so its N stays on real north.
  $("compass-rose").style.transform = `rotate(${-map.getBearing()}deg)`;

  const origin = compassOrigin();
  const result = compassSectors(origin);
  const readout = $("compass-readout");
  $("compass-foot").textContent = origin.away
    ? `From map centre — ${origin.away}`
    : `Nearby cells, from ${origin.from}`;

  if (!result) {
    box.dataset.state = "none";
    renderCompassWedges(null);
    readout.textContent = origin.from === "your location"
      ? "You are just outside this city's data."
      : "Move the map over a city.";
    return;
  }

  renderCompassWedges(result.sectors);
  const ranked = result.sectors
    .map((s, i) => ({ ...s, index: i }))
    .filter((s) => s.mean !== null)
    .sort((a, b) => a.mean - b.mean);
  const best = ranked[0];

  if (!best || best.mean >= result.here * (1 - COMPASS_MARGIN)) {
    box.dataset.state = "none";
    readout.innerHTML = result.here === 0
      ? html`<b>Already quiet</b><span class="compass-detail">nothing lower nearby</span>`
      : html`<b>No clear direction</b><span class="compass-detail">nothing nearby is meaningfully lower</span>`;
    box.setAttribute("aria-label", "Lower-crime direction: none nearby");
    return;
  }

  box.dataset.state = "point";
  $("compass-needle").style.transform = `rotate(${best.index * 45}deg)`;
  const drop = result.here > 0 ? 1 - best.mean / result.here : null;
  readout.innerHTML = html`<b>Head ${best.point}</b>${
    drop === null ? "" : html`<span class="compass-detail">about ${Math.round(drop * 100)}% lower than here</span>`
  }`;
  box.setAttribute(
    "aria-label",
    `Lower-crime direction: ${COMPASS_NAMES[best.point]}` +
      (drop === null ? "" : `, about ${Math.round(drop * 100)}% lower than here`)
  );
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
  return cities;
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

function initCityMarkers() {
  map.addSource("cities", { type: "geojson", data: cityMarkerData() });
  map.addLayer({
    id: "city-dots",
    type: "circle",
    source: "cities",
    maxzoom: CITY_MARKER_MAX_ZOOM,
    paint: {
      "circle-radius": 7,
      "circle-color": SERIES_1,
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 2,
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
    paint: { "text-color": INK_SECONDARY, "text-halo-color": "#ffffff", "text-halo-width": 1.5 },
  });
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

function renderProgress(body) {
  const view = progressView(body);
  const box = $("load-progress");
  box.hidden = !view.show;
  if (!view.show) return;
  $("lp-text").textContent = view.text;
  $("lp-detail").textContent = view.detail;
  $("lp-fill").style.width = `${view.percent}%`;
  const bar = $("lp-bar");
  bar.setAttribute("aria-valuenow", String(Math.round(view.percent)));
  bar.setAttribute("aria-valuetext", view.text);
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

/** Open a cell's popup: its rating now, and its hour and weekday shapes. */
async function selectCell(h3Index) {
  if (state.selected && state.selected !== h3Index) {
    map.setFeatureState({ source: "cells", id: state.selected }, { selected: false });
  }
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
}

/** The popup's clock: the cell's city's exact time now, "10:13 PM EDT". */
function renderCityClock() {
  const popupTime = document.querySelector(".cell-popup .cp-time");
  if (popupTime && state.selected) {
    popupTime.textContent = cityTimeLabel(cellCity(state.selected)?.timezone, new Date(), { zoneName: true });
  }
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
 * "Lower-risk window: 1 PM – 4 PM — about 18% lower than now": plain text.
 *
 * Compared with right now, not the day's average: at 9 AM every stretch in
 * the next twelve hours is daytime and above average, so an average-based
 * figure would call the "lower-risk" window "24% above average". When even
 * the quietest stretch is still busier than the day as a whole, it says so.
 */
function windowSentence(win) {
  if (win.startsNow) {
    return `Lower-risk window: now until ${hourName(win.end)} — the quietest stretch of the next 12 hours`;
  }
  if (!win.quieter) return "No meaningfully quieter stretch in the next 12 hours";
  const still = win.aboveAverage ? ", though still busier than its daily average" : "";
  return `Lower-risk window: ${hourName(win.start)} – ${hourName(win.end)} — ` +
    `about ${pctOf(win.vsNow)} lower than now${still}`;
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

/* ---------------------------------------------------------------- cell popup */

/* The whole cell view: the relative risk rating now, and the cell's day and
   week against themselves, in a small box on the map. */
let cellPopup = null;

/** "#rrggbb" for a multiple of the city average, by the same linear
    interpolation the fill's `interpolate` expression makes over
    valueRampStops, so the outline matches the cell it belongs to. */
function colorForRatio(ratio) {
  if (ratio === null || ratio === undefined || ratio < 0) return ZERO_FILL;
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

function removeCellPopup() {
  const popup = cellPopup;
  cellPopup = null;
  popup?.remove();
}

function showCellPopup(h3Index, detail) {
  removeCellPopup();

  const props = state.featureIndex.get(h3Index);
  const ratio = ratioNow(props);
  const city = cellCity(h3Index);
  const zone = city?.timezone;
  const { hour: nowHour, day: nowDay } = cityNow(city);

  // The rating is the same multiple of its city's average the cell's colour
  // (and so the outline) comes from: the smoothed, severity-weighted rate the
  // ranking is built on, at the hour and weekday it is now in that city.
  const windowLabel = detail.window_label.toLowerCase();
  const track = TRACK_LABELS[state.track];
  const sense = ratio === null ? ""
    : ratio > 1.05 ? "higher than the city average"
    : ratio < 0.95 ? "lower than the city average"
    : "about the city average";
  // "Saturday at 10 PM": the time the rating is for -- now, in the cell's city.
  const at = `${dayName(nowDay)} at ${hourName(nowHour)} · `;
  const rating = ratio === null
    ? {
        value: "—",
        label: `no ${track} risk rating for this cell · ${at}${windowLabel}`,
      }
    : {
        value: formatRatio(ratio),
        label: `relative ${track} risk — ${sense} in ${city?.city_name ?? "this city"} · ${at}${windowLabel}`,
      };

  const counts = detail.hour_profile?.[state.track];
  const max = Math.max(...(counts ?? []), 1);
  const now = nowHour;
  // The 12-hour outlook: the quietest three-hour stretch ahead, from this
  // cell's own day on this track (hours.js). Its bars go green only when it
  // is worth pointing at -- it starts now, or it is meaningfully lower.
  const win = counts ? quietestWindow(counts, now) : null;
  const safer = new Set(win && (win.startsNow || win.quieter) ? win.blocks : []);
  const outlook = win
    ? windowSentence(win)
    : "Too few incidents here for a 12-hour outlook.";
  const span = (hour) => `${hourName(hour)} – ${hourName(hour + 1)}`;
  const chart = counts && counts.some((n) => n > 0)
    ? html`<div class="cp-sub cp-time">${cityTimeLabel(zone, new Date(), { zoneName: true })}</div>
      <div class="hours" role="img" aria-label="Reported ${TRACK_LABELS[state.track]} offences by hour of the day">${counts.map((n, hour) => {
        const height = Math.max((n / max) * 100, n > 0 ? 4 : 0);
        const on = n > 0 ? html` data-on` : "";
        const current = hour === now ? html` data-selected` : "";
        const quiet = safer.has(hour) ? html` data-safer` : "";
        return html`<i style="height:${height}%"${on}${quiet}${current} title="${span(hour)}: ${nf.format(n)}"></i>`;
      })}</div>
      <div class="hours-axis"><span>12 AM</span><span>6 AM</span><span>12 PM</span><span>6 PM</span><span>11 PM</span></div>
      <div class="cp-outlook">${outlook}</div>
      <div class="cp-foot">Dark bar: now in the city.${safer.size ? " Green bars: the lower-risk window." : ""}
        The usual pattern here, not a forecast.</div>`
    : html`<div class="cp-foot">No incidents here with a recorded hour.</div>`;

  // The week: this cell's incidents on each weekday, today's bar dark -- the
  // day the rating above is for when the map is on today.
  const days = detail.day_profile?.[state.track];
  const today = nowDay;
  const week = days && days.some((n) => n > 0)
    ? (() => {
        const most = Math.max(...days, 1);
        const vs = dayVsUsual(days, today);
        const sentence = !vs
          ? "Too few incidents here to compare days of the week."
          : vs.direction === "flat"
            ? `${dayName(today)}s here are about an average day.`
            : `${dayName(today)}s here see ${pctOf(vs.change)} ${vs.direction === "up" ? "more" : "fewer"} ` +
              `${TRACK_LABELS[state.track]} incidents than an average day.`;
        return html`<div class="cp-sub">By day of the week</div>
          <div class="hours days" role="img" aria-label="Reported ${TRACK_LABELS[state.track]} offences by day of the week">${days.map((n, day) => {
            const height = Math.max((n / most) * 100, n > 0 ? 4 : 0);
            const on = n > 0 ? html` data-on` : "";
            const current = day === today ? html` data-selected` : "";
            return html`<i style="height:${height}%"${on}${current} title="${dayName(day)}: ${nf.format(n)}"></i>`;
          })}</div>
          <div class="days-axis">${days.map((_, day) => html`<span>${dayName(day).slice(0, 3)}</span>`)}</div>
          <div class="cp-outlook">${sentence}</div>`;
      })()
    : "";

  const box = document.createElement("div");
  box.className = "cp";
  box.innerHTML = html`<div class="cp-value">${rating.value}</div>
    <div class="cp-label">${rating.label}</div>${chart}${week}`;

  const [lat, lng] = h3.cellToLatLng(h3Index);
  // closeOnClick off: the map's own click handler decides, so clicking a
  // second cell swaps the popup instead of racing a close against the open.
  const popup = new maplibregl.Popup({ closeOnClick: false, maxWidth: "240px", className: "cell-popup" })
    .setLngLat([lng, lat])
    .setDOMContent(box)
    .addTo(map);
  popup.getElement().style.setProperty("--cell-color", colorForRatio(ratio));
  popup.on("close", () => {
    // Only the close button gets here with this popup still current.
    if (cellPopup === popup) closeDetail();
  });
  cellPopup = popup;
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

/**
 * Keep the compass on the visitor as they move, once they have shared a
 * location. Only the compass follows -- the map and the open cell stay where
 * the visitor put them.
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
      renderCompass();
    },
    () => { /* keep the last fix */ },
    { enableHighAccuracy: true, maximumAge: 15000 }
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
const isNarrow = () => window.matchMedia("(max-width: 600px)").matches;

/** Camera padding that keeps a target clear of the floating controls: the
    search box above it on a phone, the directions sheet below it once open,
    the column down the left edge on a wide screen. */
function clearOfControls() {
  const gap = 24;
  if (!isNarrow()) {
    return dir.open ? { top: 60, bottom: 60, right: 60, left: 360 } : { top: 0, bottom: 0, left: 0, right: 0 };
  }
  const top = $("search-field").getBoundingClientRect().bottom + gap;
  const sheet = $("dir-panel");
  const bottom = dir.open && !sheet.hidden ? window.innerHeight - sheet.getBoundingClientRect().top + gap : gap;
  return { top, bottom, left: gap, right: gap };
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
    list.innerHTML = html`${next.map(
      (o, i) => html`<li role="option" id="${list.id}-opt-${i}" data-index="${i}" aria-selected="false">
        <span class="opt-title">${o.title}</span>${o.subtitle ? html`<span class="opt-sub">${o.subtitle}</span>` : ""}
      </li>`
    )}`;
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

/** Pin `place`, fly to it and open its cell's popup -- if it is a cell of
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

/** Right-click on the map. The first pins that point where it is, with no
    camera move, and fills the search box with the nearest address -- a way
    to find an address by pointing at it. Once there is a destination, the
    next sets the start instead (pinClickedStart), opening the directions
    panel if it is not open; a new destination starts from Clear or a search. */
async function pinClickedPoint(lngLat) {
  const point = { lat: lngLat.lat, lng: lngLat.lng };
  if (search.place) {
    if (!dir.open) openDirections({ focus: false });
    return pinClickedStart(point);
  }
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
  pinPlace(place, cell);
}

/** Right-click once there is a destination: the same, for the start. Its A
    pin goes down at once, the start box fills with the nearest address, and
    the route is asked for. */
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

/** Make `place` the searched address: its pin, its cell's popup, and the
    directions destination when the panel is open. */
function pinPlace(place, cell) {
  search.marker?.remove();
  search.place = place;
  search.marker = searchPin().setLngLat([place.lng, place.lat]).addTo(map);
  $("search-actions").hidden = false;
  selectCell(cell);
  // An open panel routes to the new address.
  if (dir.open) {
    search.marker.remove();
    $("dir-to-label").textContent = place.label;
    clearResult();
    placeMarkers();
    if (dir.from) submitDirections();
  }
}

function clearSearchPin() {
  search.place = null;
  search.marker?.remove();
  search.marker = null;
  $("search-actions").hidden = true;
}

/** Back to no address: the box, the pin, the panel and any route. */
function resetSearch() {
  search.seq += 1;
  closeDirections();
  clearSearchPin();
  dir.fromSeq += 1;
  dir.from = null;
  $("dir-from").value = "";
  $("search-input").value = "";
  setStatus("search-status", "");
}

/* ---- the directions panel */

/** `focus: false` leaves the start box unfocused -- for a right-click, which
    fills it, so a phone does not raise its keyboard. */
function openDirections({ focus = true } = {}) {
  if (!search.place) return;
  dir.open = true;
  $("dir-panel").hidden = false;
  document.body.classList.add("dir-open");
  $("btn-directions").setAttribute("aria-expanded", "true");
  $("dir-to-label").textContent = search.place.label;
  // The route takes the map: the popup would sit on top of it, and the
  // searched pin becomes the B end.
  closeDetail();
  search.marker?.remove();
  placeMarkers();
  if (dir.result) drawRoutes();
  if (focus) $("dir-from").focus();
}

function closeDirections() {
  endNavigation();
  if (!dir.open) return;
  dir.open = false;
  $("dir-panel").hidden = true;
  document.body.classList.remove("dir-open");
  $("btn-directions").setAttribute("aria-expanded", "false");
  clearResult();
  clearRouteLayers();
  if (search.marker) search.marker.addTo(map);
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
  clearRouteLayers({ keepMarkers: true });
}

function endMarker(end) {
  const el = document.createElement("div");
  // MapLibre positions the marker with its own transform, so the turn that
  // makes the pin's point goes on an inner element.
  el.className = "route-pin-marker";
  const pin = document.createElement("div");
  pin.className = `route-pin route-pin-${end}`;
  const letter = document.createElement("span");
  letter.textContent = end === "from" ? "A" : "B";
  pin.append(letter);
  el.append(pin);
  el.setAttribute("aria-hidden", "true");
  // The pin is a square turned 45°, its point 18 px (half the 26 px
  // diagonal) below its centre: lift it so the point, not the middle, sits on
  // the place.
  return new maplibregl.Marker({ element: el, anchor: "center", offset: [0, -18] });
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
  if (place) submitDirections();
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
  setFrom({ ...place, label: point.label === undefined ? `Your location (${place.label})` : place.label });
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
  setStatus(
    "dir-status",
    `${n} ${MODE_NAMES[result.mode].toLowerCase()} route${n === 1 ? "" : "s"} found · ` +
      `fastest ${formatDuration(result.routes[result.picks?.[state.track]?.fastest ?? 0].duration_s)}`
  );
}

function renderDirections() {
  const result = dir.result;
  if (!result) return;
  const track = TRACK_LABELS[state.track];
  const cards = routeCardModel(result, state.track);

  $("dir-results").hidden = false;
  $("dir-track").textContent =
    `Compared on ${track} offences (the map's selected ranking), against ` +
    `${result.window?.label?.toLowerCase() ?? "the city"}` +
    (result.daily
      ? ", at the day and hour each stretch would be passed."
      : result.hourly ? ", at the hour each stretch would be passed." : ", all hours.");

  $("dir-cards").innerHTML = html`${cards.map((card) => {
    const route = result.routes[card.index];
    const words = activitySentence(card.activity, track);
    const swatch = card.activity?.comparable ? colorForRatio(card.activity.mean_ratio) : ZERO_FILL;
    const legs = route.mode === "transit" ? route.legs.map(transitLegSummary) : [];
    return html`<li>
      <button type="button" class="route-card" data-index="${card.index}"
              aria-pressed="${String(card.index === dir.selected)}">
        <span class="route-role">${card.role}${card.extra ? html` <span class="route-extra">${card.extra}</span>` : ""}</span>
        <span class="route-main"><strong>${card.duration}</strong> · ${card.distance}${
          route.mode === "transit" && route.start_time
            ? html` · leaves ${clockIn(route.start_time, result.timezone)}`
            : ""
        }</span>
        ${legs.length ? html`<span class="route-legs">${legs.map((leg) =>
          leg.kind === "ride"
            ? html`<span class="leg-chip" style="${leg.color ? `background:${leg.color};color:${leg.textColor}` : ""}">${leg.name}</span>`
            : html`<span class="leg-walk">${leg.text.split(" ")[0]}</span>`
        )}</span>` : ""}
        <span class="route-activity"><span class="swatch" style="background:${swatch}" aria-hidden="true"></span>${words.headline}</span>
        <span class="route-detail">${words.detail}</span>
      </button>
    </li>`;
  })}`;

  // Why no slower route is the lower-activity pick -- only true, and only
  // shown, when the fastest is on top.
  const fastestOnTop = cards[0].index === (result.picks?.[state.track]?.fastest ?? 0);
  $("dir-why").textContent = fastestOnTop ? pickReason(result.picks?.[state.track], result.mode) ?? "" : "";
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
  closeDetail();
  nav.watch = navigator.geolocation.watchPosition(onNavFix, onNavError, {
    enableHighAccuracy: true,
    maximumAge: 0,
    timeout: 20000,
  });
  holdScreenOn();
  renderNav();
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

/** The panel while navigating: what is left, and the steps still ahead. */
function renderNav() {
  if (!nav.active) return;
  const route = nav.route;
  const street = route.mode !== "transit";
  const arrived = nav.index >= nav.steps.length;
  const offRoute = !arrived && nav.off >= NAV_OFF_FIXES;

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
  $("nav-left").textContent = arrived ? "Arrived" : formatDistance(left);
  if (arrived) $("nav-eta").textContent = "";
  else if (route.end_time) $("nav-eta").textContent = `· arrive ${clockIn(route.end_time, dir.result?.timezone)}`;
  else {
    const speed = route.duration_s > 0 ? route.distance_m / route.duration_s : 0;
    $("nav-eta").textContent = speed > 0 ? `· ${formatDuration(left / speed)}` : "";
  }

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
function initRouteLayers() {
  const empty = { type: "FeatureCollection", features: [] };
  for (const id of ["route-alt", "route-line", "route-activity"]) {
    map.addSource(id, { type: "geojson", data: empty });
  }
  // The first label above the last road: the basemap puts some water labels
  // before its roads, and a route under the roads is hidden by them.
  const layers = map.getStyle().layers;
  const lastRoad = layers.findLastIndex((layer) => /^(tunnel|road|bridge|highway)/.test(layer.id));
  const before = layers.slice(lastRoad + 1).find((layer) => layer.type === "symbol")?.id;
  map.addLayer({
    id: "route-alt-line", type: "line", source: "route-alt",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": INK_SECONDARY, "line-width": 4, "line-opacity": 0.6, "line-dasharray": [1.5, 1.5] },
  }, before);
  // A dark casing: the route sits on cells painted in the same ramp it is
  // coloured with, and only an edge keeps a green route off a green cell.
  map.addLayer({
    id: "route-casing", type: "line", source: "route-line",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#1b1b1a", "line-width": 10, "line-opacity": 0.55 },
  }, before);
  map.addLayer({
    id: "route-line", type: "line", source: "route-line",
    filter: ["!", ["get", "walk"]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["coalesce", ["get", "color"], SERIES_1], "line-width": 6 },
  }, before);
  map.addLayer({
    id: "route-walk", type: "line", source: "route-line",
    filter: ["get", "walk"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": SERIES_1, "line-width": 6, "line-dasharray": [0.1, 1.8] },
  }, before);
  // The reported activity along the route, on the map's own ramp, drawn over
  // the plain line. Only scored stretches have segments, so a transit ride
  // keeps its line colour between the walks.
  map.addLayer({
    id: "route-activity", type: "line", source: "route-activity",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": [
        "case", ["<", ["get", "ratio"], 0], "#9b9a94",
        ["interpolate", ["linear"], ["get", "ratio"], ...valueRampStops(VALUE_RAMP)],
      ],
      "line-width": 6,
    },
  }, before);

  map.on("click", "route-alt-line", (event) => {
    const alt = event.features?.[0];
    if (alt && dir.open) selectRoute(alt.properties.index);
  });
  map.on("mouseenter", "route-alt-line", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "route-alt-line", () => {
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
    closeDetail();
    $("search-input").focus();
  };
  $("btn-directions").onclick = () => (dir.open ? closeDirections() : openDirections());
  $("dir-start").onclick = startNavigation;
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
    $("btn-directions").focus();
  };

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
      const point = await getPosition();
      state.userLocation = point;
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

async function initMap() {
  const style = await resolveStyle();
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

  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ maxWidth: SCALE_MAX_WIDTH, unit: "imperial" }), "bottom-right");

  await new Promise((resolve) => map.on("load", resolve));

  map.addSource("cells", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
    promoteId: "h3",
  });

  map.addLayer({
    id: "cells-fill",
    type: "fill",
    source: "cells",
    paint: { "fill-color": ZERO_FILL, "fill-opacity": cellsOpacity() },
  }, cellsBeforeId());

  // Building footprints are fills the basemap draws after its roads. Left above
  // the cells they would grey the colour out block by block at street zoom, so
  // they go underneath with the land and water.
  const layers = map.getStyle().layers;
  const cellsAt = layers.findIndex((layer) => layer.id === "cells-fill");
  for (const layer of layers.slice(cellsAt + 1)) {
    if (layer.type === "fill") map.moveLayer(layer.id, "cells-fill");
  }

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
    // A click on another route picks that route, not the cell under it.
    if (dir.open && map.queryRenderedFeatures(event.point, { layers: ["route-alt-line"] }).length) return;
    const feature = event.features?.[0];
    if (feature) selectCell(feature.properties.h3);
  });
  map.on("click", (event) => {
    const onCell = map.queryRenderedFeatures(event.point, { layers: ["cells-fill"] }).length;
    if (!onCell) closeDetail();
  });
  // Only while address search is on: the box it fills is hidden otherwise.
  map.on("contextmenu", (event) => {
    // Not while navigating: a new pin would end the trip being followed.
    if (!$("search-field").hidden && !nav.active) pinClickedPoint(event.lngLat);
  });
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
  map.on("rotate", () => {
    $("compass-rose").style.transform = `rotate(${-map.getBearing()}deg)`;
  });

  initRouteLayers();
  initCityMarkers();
  window.__safetyMap = map;
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
  return [
    "case",
    ["boolean", ["feature-state", "selected"], false], 0.95,
    ["boolean", ["feature-state", "hover"], false], 0.9,
    0.74,
  ];
}

/** The track toggle picks the colour the cells are painted in, so it shows
    only while the tiles are on. */
function syncTrackField() {
  $("f-track-field").hidden = !state.tiles;
}

function setTiles(on) {
  state.tiles = on;
  $("f-tiles")
    .querySelectorAll("[data-tiles]")
    .forEach((b) => b.setAttribute("aria-checked", String((b.dataset.tiles === "on") === on)));
  map.setPaintProperty("cells-fill", "fill-opacity", cellsOpacity());
  syncTrackField();
}

/* The only two controls: Show tiles, and which offences the colour ranks.
   There is nothing else to choose -- every city, the last year, coloured for
   the hour and day it is now in each city. */
function wireControls() {
  $("f-tiles").querySelectorAll("[data-tiles]").forEach((button) => {
    button.onclick = () => setTiles(button.dataset.tiles === "on");
  });

  // Both tracks are rated on every feature (rateCity), so switching is a
  // repaint with no request and no loading state.
  $("f-track").querySelectorAll("[data-track]").forEach((button) => {
    button.onclick = () => {
      state.track = button.dataset.track;
      $("f-track")
        .querySelectorAll("[data-track]")
        .forEach((b) => b.setAttribute("aria-selected", String(b === button)));
      repaint();
      // The popup's rating, outline and charts are per track too, and the
      // detail is already in hand -- re-read it rather than re-request it.
      if (state.detail && state.selected) showCellPopup(state.selected, state.detail);
      // Both tracks ride on every route too.
      if (dir.result) renderDirections();
    };
  });

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
