/* ---------------------------------------------------------------------------
   Temporary Phase 1 front end.

   Reads only the precomputed gold-layer endpoints -- it never asks the server
   to aggregate anything (design doc S9.3). The one computation it does locally
   is H3 cell membership from GPS coordinates, which S10 points out is a pure
   function of (lat, lng, resolution) and needs no server round trip at all.
   --------------------------------------------------------------------------- */

const API = "/api/v1";

/* The city to open on, when the URL does not say. Not "the only city": the
   picker is populated from /api/v1/cities, and this is only the fallback if that
   city is among them. Otherwise the first one served wins. */
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
 * City average of `prop` across the cells this view actually paints.
 *
 * Computed here rather than served, for the same reason the hourly count rank
 * is: the features are already in the browser, and the alternative is another
 * precomputed table existing only to colour one view. Cells that render in the
 * neutral fill are excluded -- they never take a colour from the ramp, so
 * letting them drag the average down would turn the rest of the city orange.
 *
 * A rate per person is averaged per person, weighted by each cell's exposure,
 * so the average is the city's rate and not the mean of its cells' rates -- the
 * same sense `cityExposureRate` gives the panel's percentage. A count has no
 * denominator to weight by, and neither does a layer ranked by area, so those
 * take a plain mean.
 */
function valueDomain(prop, isPainted, perPerson) {
  let sum = 0;
  let painted = 0;
  let weighted = 0;
  let people = 0;
  for (const feature of state.features) {
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

/**
 * Citywide severity-weighted offence per ambient person on one safety track,
 * for the layer on screen. Per track, like the ranking: a violent and a
 * non-violent offence are never added together.
 *
 * Summed here rather than served for the same reason `valueDomain` is: the
 * features are already in the browser, and the alternative is another
 * precomputed figure existing only to fill one line of one panel. The activity
 * layer is dense and the client fetches it unfiltered, so this is the whole city
 * and not a sample of it.
 *
 * Numerator and denominator are accumulated over the same cells -- the ones
 * carrying a population figure. Counting incidents from a cell whose exposure is
 * unknown would add to the top of the fraction without adding to the bottom, and
 * make the city look worse than it is.
 */
function cityExposureRate(weightedProp) {
  let weighted = 0;
  let people = 0;
  for (const feature of state.features) {
    const p = feature.properties;
    if (typeof p.exposure !== "number" || p.exposure <= 0) continue;
    weighted += p[weightedProp] ?? 0;
    people += p.exposure;
  }
  return people > 0 ? { weighted, people, rate: weighted / people } : null;
}

/* Below this many ambient people, a rate per person is two small numbers
   divided: a cell with nine people and three offences comes out at twenty-four
   times the city average, which is arithmetic rather than a finding. */
const RELATIVE_MIN_EXPOSURE = 100;

/**
 * One cell's severity-weighted offence per person on the selected track,
 * against the city's own on that track.
 *
 * Read out of `state.features` rather than out of the detail payload, because
 * the feature is what is on screen. Both sides of the ratio then come from one
 * layer and share a denominator definition -- the same ambient
 * residents-and-jobs figure the safety ranking itself divides by.
 *
 * Returns a `reason` instead of a ratio wherever the division would mislead;
 * `relativeStat` turns each of those into its own sentence.
 */
function relativeRate(h3) {
  const feature = state.features.find((f) => f.properties.h3 === h3);
  if (!feature) return { reason: "unavailable" };

  // Not one cell in this layer carries a population denominator: either the
  // active severity scheme ranks by area, in which case cell_safety.exposure is
  // NULL by construction, or the exposure layer was never built. There is
  // nothing to be relative to, so the headline falls back to the count -- the
  // same fallback syncSafetyAvailability makes for the ramp.
  const { weighted: weightedProp } = TRACK_PROPS[state.track];
  const city = cityExposureRate(weightedProp);
  if (!city || city.rate <= 0) return { reason: "no_city_exposure" };

  const p = feature.properties;
  const weighted = p[weightedProp] ?? 0;
  const exposure = typeof p.exposure === "number" ? p.exposure : null;

  if (exposure === null || exposure <= 0) return { reason: "no_exposure", weighted };
  if (weighted === 0) return { reason: "no_incidents", weighted, exposure, city };
  if (exposure < RELATIVE_MIN_EXPOSURE) {
    return { reason: "thin_exposure", weighted, exposure, city };
  }
  const rate = weighted / exposure;
  return { ratio: rate / city.rate, rate, weighted, exposure, city };
}

/** The ratio in whichever form reads as a quantity: 1,400% has to be decoded,
    14.0x does not.

    The bottom end is named rather than rounded, the same way `safetyLabel`
    names its extremes: a big quiet cell -- one report among twenty thousand
    people -- rounds to 0%, and 0% is the one thing this figure must not say
    about a cell where something was reported. */
function formatRatio(ratio) {
  if (ratio >= 10) return `${ratio.toFixed(1)}×`;
  if (ratio < 0.005) return "<1%";
  return `${Math.round(ratio * 100)}%`;
}

/* Per 1,000 people, matching every other exposure-denominated figure here. */
const per1k = (rate) => (rate * 1000).toFixed(1);

/**
 * The safety view's headline figure, in words.
 *
 * A percentage of the city's rate, never a bare one: above 100% is the
 * concerning direction, which is the opposite of what a number under a heading
 * reading "safety" would be assumed to mean, so the sense is always spelled out
 * beside it. The two rates it came from go underneath, because a derived
 * figure is only checkable if the numbers behind it are visible.
 */
function relativeStat(rel, windowLabel) {
  const track = TRACK_LABELS[state.track];
  switch (rel.reason) {
    case "no_incidents":
      return {
        value: "—",
        label: `no ${track} offences reported here · ${windowLabel}`,
        note:
          `0 ${track} offences among ${nf.format(rel.exposure)} people. An absence ` +
          `of reports is not evidence of safety.`,
      };
    case "thin_exposure":
      return {
        value: "—",
        label: `too few people here to compare · ${windowLabel}`,
        note:
          `${nf.format(Math.round(rel.weighted))} weighted ${track} offence among ` +
          `${nf.format(rel.exposure)} people — a rate per person on a denominator ` +
          `this small would swing on a single report.`,
      };
    case "no_exposure":
      return {
        value: "—",
        label: "no population figure for this cell",
        note:
          `${nf.format(Math.round(rel.weighted))} weighted ${track} offence. The ` +
          `comparison divides by ambient population, which this cell has none ` +
          `apportioned to it.`,
      };
    case "unavailable":
      return {
        value: "—",
        label: "not comparable in this layer",
        note: "",
      };
  }

  // A ±5% deadband: inside it, the honest reading is "no different", not a
  // number to two figures.
  const sense =
    rel.ratio > 1.05 ? "higher than the city average"
    : rel.ratio < 0.95 ? "lower than the city average"
    : "about the city average";

  return {
    value: formatRatio(rel.ratio),
    label: `of the city-average ${track} offence rate per person — ${sense} · ${windowLabel}`,
    note:
      `Severity-weighted · ${per1k(rel.rate)} vs ${per1k(rel.city.rate)} per 1,000 ` +
      `people citywide`,
  };
}

const TIER_LABELS = {
  0: "No reported incidents",
  1: "Lowest fifth",
  2: "Lower-middle fifth",
  3: "Middle fifth",
  4: "Upper-middle fifth",
  5: "Highest fifth",
};

const SAFETY_TIER_LABELS = {
  0: "No reported incidents",
  1: "Least safe quarter",
  2: "Lower-middle quarter",
  3: "Upper-middle quarter",
  4: "Safest quarter",
};

const TRACK_LABELS = {
  violent: "violent",
  non_violent: "non-violent",
};

/* Feature property names per track, so the map reads whichever is selected
   without refetching -- both ship on every feature. The h* pair is the same
   measure inside the selected hour block, and is null unless one was asked for.
   Switching track stays a repaint; switching hour is a refetch, because
   carrying all 24 blocks on every feature would multiply the payload by 24. */
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

/* Mirrors safety.etl.gold.HOURLY_RESOLUTIONS. The API rejects anything outside
   this with a reason; the client knows the same bounds so it can disable the
   control up front rather than let a request fail. Which *windows* carry the
   hourly layer comes with each city's window list (windows.js). */
const HOURLY_RESOLUTIONS = [8, 9];

/* Mirrors safety.etl.gold.SAFETY_RESOLUTIONS. The safety ranking divides by
   ambient population apportioned from census blocks, and a res-10 cell is
   smaller than a census block -- there is no population figure at that size
   that is not interpolation. Counts are still served there, so the map falls
   back to the count ramp rather than going blank. */
const SAFETY_RESOLUTIONS = [8, 9];

/* Mirrors safety.etl.gold.ACTIVITY_CATEGORIES. The activity layer is dense --
   one row per cell per window per category, since a cell with no reported
   incidents is still part of the distribution it is ranked against -- so
   resolution 10 is built for the combined category only, and for the one- and
   two-year windows (each window's `res10` flag). At ~0.015 km² a single
   category over 30 days leaves nearly every cell on zero, tied with every
   other, and a percentile over a field of ties is not a reading. Same bounds on
   the server, which answers the rest with the reason; the client knows them so
   the controls can say so first. */
const ACTIVITY_CATEGORIES = { 10: ["all"] };

const activityCategories = (res) => ACTIVITY_CATEGORIES[res] ?? null;

/** The stored window the selected dates are exactly, or null for a custom
    range (and before the city record loads). */
const currentPreset = () =>
  presetFor(state.range, state.cityRecord?.windows, state.cityRecord?.coverage_end);

const todayIso = () => localIso(new Date());

/** ?from=&to= from a shared link, or null. */
function initialRange() {
  const params = new URLSearchParams(location.search);
  const iso = /^\d{4}-\d{2}-\d{2}$/;
  const from = params.get("from");
  const to = params.get("to");
  return iso.test(from ?? "") && iso.test(to ?? "") ? clampRange({ from, to }) : null;
}

/** The request's period. A preset goes by its window id, so it is served from
    the stored build (hourly layer and all); anything else goes as the dates. */
function periodParams() {
  const preset = currentPreset();
  return preset ? { window: preset.id } : { from: state.range.from, to: state.range.to };
}

/** "20:00–21:00". The last block reads 23:00–24:00, not 23:00–00:00. */
const hourLabel = (hour) =>
  `${String(hour).padStart(2, "0")}:00–${String(hour + 1).padStart(2, "0")}:00`;

const CATEGORY_LABELS = {
  violent: "Violent",
  property: "Property",
  quality_of_life: "Quality of life",
  other: "Other",
};

const state = {
  /* Set during boot from ?city=, the visitor's city, or the served list; never assumed. */
  city: null,
  /* Every served city's record from /api/v1/cities, bounding boxes included,
     so "which city am I in" needs no request of its own. */
  cities: [],
  /* The selected city's snapshot row, so the header, the frame and the
     methodology sheet all read from one place. */
  cityRecord: null,
  /* The two dates the map shows, {from, to} as ISO strings (windows.js).
     ?from=&to= win on first load; otherwise renderRangeControls fills it in
     once the city record arrives -- from a legacy ?window= link if there is
     one, else the year up to today. */
  range: initialRange(),
  pendingWindow: new URLSearchParams(location.search).get("window"),
  // Set across a city switch when the dates were a preset (selectCity).
  carryPreset: null,
  category: "all",
  res: 8,
  scale: "safety",
  track: "violent",
  // The selected hour block 0-23 on the city's clock, or null for all hours.
  // Picked in the time-of-day control; cleared by syncHourAvailability where
  // the window or cell size has no hourly layer.
  hour: null,
  selected: null,
  hovered: null,
  meta: null,
  features: [],
  // h3 -> properties for the layer on screen, for the compass's ring lookups.
  featureIndex: new Map(),
  // {lat, lng} once the visitor shares a location; the compass reads from it.
  userLocation: null,
  // The open cell's detail payload, so a track switch re-reads rather than refetches.
  detail: null,
  // City average the ramp is currently pinned to, so the compass reads cells
  // on the same scale the fill paints them with.
  rampDomain: null,
  refreshStamp: null,
  framed: false,
  // Layer requests are numbered and the previous one aborted, so a slow
  // response for controls the user has since changed can never overwrite the
  // layer they now describe.
  layerSeq: 0,
  layerAbort: null,
};

const nf = new Intl.NumberFormat("en-US");
const $ = (id) => document.getElementById(id);

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
 * Paint expression for both modes, on a value scale pinned to the city average.
 *
 * Both modes now run the ramp low-value-green to high-value-red, so red is the
 * concerning end in either and switching between them is a change of measure
 * rather than a change of vocabulary. The safety mode's value is the smoothed
 * severity-weighted rate -- the quantity its ranking is built on, so colour and
 * percentile order cells identically even though only one of them is a rank.
 *
 * Every cell is coloured by its multiple of the city average rather than by the
 * raw value, so a cell borrowing its all-hours standing (see `paintedMeasure`)
 * sits on the same bar as the cells rated at this hour.
 *
 * The chosen domain is stashed on `state` for the compass, so it can never
 * read a cell on a different scale from the one the fill uses.
 */
/** The property the map is coloured by right now, and which cells take a
    colour from the ramp. Shared by the fill and the compass, so the compass
    can never point along a different measure from the one on screen.

    `fallback` is the all-hours measure the safety view falls back to for a
    cell with nothing of this track reported. The hourly rate is the all-hours
    one times an hour factor, so its tier 0 is the all-hours tier 0: a cell with
    nothing reported at any hour. Its rate is still a ranking, built from its
    neighbours and the city baseline, so it takes that smoothed rate rather than
    going grey -- as does a cell with no hourly row at all. Only a cell with no
    rate anywhere stays in the neutral fill. */
function paintedMeasure() {
  const props = TRACK_PROPS[state.track];
  const hourly = state.hour !== null;

  const valueProp = state.scale === "safety"
    ? (hourly ? props.hrate : props.rate)
    : (hourly ? "hcount" : "count");
  const tier = hourly ? props.htier : props.tier;
  const isPainted = state.scale === "safety"
    // Nothing of this kind reported is a separate state, not the safe end of
    // the scale: an absence of reports is not evidence of safety.
    ? (p) => (p[tier] ?? 0) !== 0
    : (p) => (p[valueProp] ?? 0) > 0;
  const fallback = state.scale === "safety"
    ? { valueProp: props.rate, tier: props.tier }
    : null;
  return { valueProp, tier, isPainted, fallback };
}

/** City averages for a measure and its fallback, plus how many cells borrow. */
function measureDomain(measure) {
  const { valueProp, isPainted, fallback } = measure;
  const own = valueDomain(valueProp, isPainted, state.scale === "safety");
  if (!fallback) return own && { own, overall: null, borrowed: 0 };

  // The average is taken over cells with reports of their own, so borrowing
  // cells are placed on the scale without moving it.
  const ranked = (p) => (p[fallback.tier] ?? 0) !== 0;
  const overall = valueDomain(fallback.valueProp, ranked, true);
  if (!own && !overall) return null;
  const borrowed = overall
    ? state.features.filter(
        (f) => !isPainted(f.properties) && typeof f.properties[fallback.valueProp] === "number"
      ).length
    : 0;
  return { own, overall, borrowed };
}

/** A cell's multiple of its city average, or null for the neutral fill. The
    JS twin of the ratio in `fillColorExpression`, for the compass. */
function cellRatio(p, measure, domain) {
  if (!domain) return null;
  const { valueProp, isPainted, fallback } = measure;
  if (isPainted(p)) return domain.own ? (p[valueProp] ?? 0) / domain.own.average : null;
  if (fallback && domain.overall && typeof p[fallback.valueProp] === "number") {
    return p[fallback.valueProp] / domain.overall.average;
  }
  return null;
}

function fillColorExpression() {
  const measure = paintedMeasure();
  const { valueProp, tier, fallback } = measure;

  const domain = measureDomain(measure);
  state.rampDomain = domain;

  if (!domain) return ZERO_FILL;

  const get = (prop) => ["coalesce", ["get", prop], 0];
  // Mirrors cellRatio: -1 marks the neutral fill, since a real ratio is never negative.
  const ratio = ["case"];
  if (domain.own) {
    ratio.push(
      state.scale === "safety" ? ["!=", get(tier), 0] : [">", get(valueProp), 0],
      ["/", get(valueProp), domain.own.average],
    );
  }
  if (domain.overall) {
    ratio.push(
      ["==", ["typeof", ["get", fallback.valueProp]], "number"],
      ["/", get(fallback.valueProp), domain.overall.average],
    );
  }
  ratio.push(-1);

  return [
    "let", "ratio", ratio,
    [
      "case",
      ["<", ["var", "ratio"], 0], ZERO_FILL,
      ["interpolate", ["linear"], ["var", "ratio"], ...valueRampStops(VALUE_RAMP)],
    ],
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
  if (!state.rampDomain) return ZERO_FILL;
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
  const city = state.cities.find((c) => c.source_id === state.city);
  if (here && city && inBbox(city, here)) return { ...here, from: "your location" };
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
  const measure = paintedMeasure();
  const ratioOf = (p) => cellRatio(p, measure, state.rampDomain) ?? 0;
  const byCell = state.featureIndex;
  const home = h3.latLngToCell(origin.lat, origin.lng, state.res);
  const homeProps = byCell.get(home);
  if (!homeProps) return null;

  const cosLat = Math.cos((origin.lat * Math.PI) / 180);
  const sectors = COMPASS_POINTS.map(() => ({ sum: 0, weight: 0, cells: 0 }));

  for (const [ring, cells] of h3.gridDiskDistances(home, COMPASS_RINGS[state.res] ?? 2).entries()) {
    if (ring === 0) continue;
    for (const cell of cells) {
      const p = byCell.get(cell);
      if (!p) continue;
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
      : "Move the map over the city.";
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

async function loadLayer({ quiet = false } = {}) {
  // A background refresh that lands while a visible load (or its error) is on
  // screen takes that load over, so it must own the indicator too: the aborted
  // load returns silently, and a quiet failure here would leave "Loading…" up (P3).
  if (quiet && !$("loading").hidden) quiet = false;
  const seq = ++state.layerSeq;
  state.layerAbort?.abort();
  const controller = (state.layerAbort = new AbortController());

  if (!quiet) {
    $("map").classList.add("is-refetching");
    // Reset first: a previous failure's message must not sit under the
    // "loading" state of the request that may fix it.
    $("loading").textContent = "Loading…";
    $("loading").classList.remove("is-error");
    $("loading").hidden = false;
  }

  // The pointer can sit still across a layer swap, so mouseleave never fires
  // and the hover highlight would stay on a cell from the previous layer.
  if (state.hovered) {
    map.setFeatureState({ source: "cells", id: state.hovered }, { hover: false });
    state.hovered = null;
  }

  const params = new URLSearchParams({
    city: state.city,
    res: String(state.res),
    ...periodParams(),
    category: state.category,
  });
  if (state.hour !== null) params.set("hour", String(state.hour));

  try {
    const response = await fetch(`${API}/cells?${params}`, { signal: controller.signal });
    if (!response.ok) {
      throw Object.assign(new Error(`cells request failed: ${response.status}`), {
        status: response.status,
      });
    }
    const collection = await response.json();
    // A newer request has been issued since; its result is the one the controls describe.
    if (seq !== state.layerSeq) return;

    state.meta = collection.metadata;
    state.features = collection.features;
    state.featureIndex = new Map(collection.features.map((f) => [f.properties.h3, f.properties]));

    const source = map.getSource("cells");
    if (source) source.setData(collection);

    map.setPaintProperty("cells-fill", "fill-color", fillColorExpression());
    renderCompass();
    renderTable();
    // The city rate the safety headline is a share of has just moved, and so has
    // the selected cell's own count if the category changed -- that control
    // reloads the layer without reopening the panel.
    if (state.detail) renderHeadlineStat(state.detail);
    // Only now is it known whether the hourly layer exists at all.
    syncHourAvailability();
    $("loading").hidden = true;
    $("loading").textContent = "Loading…";
    $("loading").classList.remove("is-error");
  } catch (error) {
    // Superseded or aborted: the newer request owns the screen.
    if (error.name === "AbortError" || seq !== state.layerSeq) return;
    // A failed background refresh (the 60 s poll) keeps the layer on screen,
    // which is still valid; it is not worth an alarm.
    if (quiet) {
      console.error(error);
      return;
    }
    console.error(error);
    // Left on screen until the next successful load replaces it -- hiding it in
    // `finally` was what made every failure look like a frozen map.
    $("loading").textContent =
      error.status === 429
        ? "Too many requests — wait a moment, then change a control to retry."
        : "Could not load cell data. Try again shortly.";
    $("loading").classList.add("is-error");
    $("loading").hidden = false;
  } finally {
    if (seq === state.layerSeq) $("map").classList.remove("is-refetching");
  }
}

async function loadFreshness({ refit = false } = {}) {
  const response = await fetch(`${API}/cities/${encodeURIComponent(state.city)}`);
  if (!response.ok) return;
  const city = await response.json();
  // A preset carries over as a preset, read against the outgoing record: after
  // a refresh "last 12 months" ends on a newer day, and another city's ends on
  // its own, so the old dates would match none of the new windows.
  // renderRangeControls finds the nearest (pickWindow). Custom dates carry over
  // as dates, clamped to what the city holds.
  const preset = currentPreset();
  state.carryPreset = preset ? { id: preset.id, span: preset.span_days } : null;
  state.cityRecord = city;
  renderRangeControls();

  // Frame the city from its own stored bounding box rather than a hardcoded
  // centre, so a second city needs no client change (design doc S11). `refit`
  // is what makes that true on a *switch* and not just on first load: without
  // it the map would stay over whichever city opened first.
  if ((refit || !state.framed) && Number.isFinite(city.bbox_west)) {
    map.fitBounds(
      [
        [city.bbox_west, city.bbox_south],
        [city.bbox_east, city.bbox_north],
      ],
      {
        padding: { top: 28, bottom: 28, left: 28, right: 28 },
        // Animate a deliberate switch, so it reads as travel rather than a cut;
        // the first frame should just be there.
        duration: state.framed && refit ? 700 : 0,
      }
    );
    state.framed = true;
  }

  document.title = `${city.city_name} reported-incident activity`;
  $("city-heading").textContent = `${city.city_name} — reported incident activity`;
  $("map").setAttribute(
    "aria-label",
    `Map of ${city.city_name} with hexagonal cells shaded by reported incident count`
  );

  // Design doc S12(b): "data as of" is a visible, first-class element.
  const asOf = new Date(city.data_as_of);
  $("data-as-of").textContent = asOf.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  $("freshness-meta").textContent =
    `· ${nf.format(city.incident_count)} incidents · updated ${city.expected_cadence}`;
}

/**
 * Populate the city picker from what the API actually serves.
 *
 * Only cities with a gold.city_snapshot row come back, which is the right set:
 * a city whose adapter exists but whose pipeline has not run has nothing to
 * show, and offering it would produce an empty map with no explanation.
 *
 * Returns the chosen source_id. `?city=` wins if it is served, then the city
 * the visitor is standing in (only where they have already granted location --
 * see cityFromGrantedLocation), then DEFAULT_CITY, then whatever is first.
 */
async function loadCities() {
  const response = await fetch(`${API}/cities`);
  if (!response.ok) throw new Error(`cities request failed: ${response.status}`);
  const { cities } = await response.json();
  if (!cities.length) throw new Error("no city has serving data yet");
  state.cities = cities;

  const select = $("f-city");
  select.replaceChildren(
    ...cities.map((city) => {
      const option = document.createElement("option");
      option.value = city.source_id;
      option.textContent = city.city_name;
      return option;
    })
  );
  // One city is not a choice; hiding the control is more honest than offering a
  // dropdown that cannot do anything.
  $("f-city-field").hidden = cities.length < 2;

  const requested = new URLSearchParams(location.search).get("city");
  const served = new Set(cities.map((c) => c.source_id));
  const chosen =
    (requested && served.has(requested) && requested) ||
    (requested ? null : await cityFromGrantedLocation()) ||
    (served.has(DEFAULT_CITY) && DEFAULT_CITY) ||
    cities[0].source_id;
  select.value = chosen;
  return chosen;
}

/**
 * Switch cities.
 *
 * Everything keyed to a place is dropped rather than carried across: an H3 index
 * belongs to exactly one city, so a selected cell, a hovered cell and a cached
 * ramp domain are all meaningless the moment the city changes. The filters --
 * dates, category, cell size, hour -- are not place-specific and do carry over,
 * which is what someone comparing two cities on the same terms would want.
 *
 * What deliberately does *not* happen is any comparison between the two. Every
 * percentile is computed against its own city's distribution (design doc S3.3),
 * so a figure from one city and a figure from another are not on the same scale
 * and the UI never places them side by side.
 */
async function selectCity(sourceId) {
  if (sourceId === state.city) return;
  state.city = sourceId;
  closeDetail();
  // An address, a start or a route in the old city means nothing in the new.
  resetSearch();
  // The stamp is per city now, so carrying the old one across would read as "the
  // pipeline just ran" on the next poll and trigger a pointless reload. Null
  // makes the next tick record rather than compare.
  state.refreshStamp = null;

  const url = new URL(location.href);
  url.searchParams.set("city", sourceId);
  history.replaceState(null, "", url);

  await loadFreshness({ refit: true });
  syncActivityScope();
  syncHourAvailability();
  await loadLayer();
}

/* --------------------------------------------------------------- cell panel */

/**
 * The panel's one big number, which measures whatever the map is coloured by.
 *
 * Under the count ramp that is the count itself. Under the safety ramp a bare
 * count is the wrong headline: the ramp is ranking cells per head of ambient
 * population, and a cell with forty incidents among twelve thousand people is
 * the quieter of two cells the count alone would order the other way. So the
 * safety view leads with the comparison the ramp is making -- this cell's
 * severity-weighted offence per person on the selected track, as a share of the
 * city's.
 */
function renderHeadlineStat(detail) {
  const value = $("d-count");
  const label = $("d-count-label");
  // "last 12 months" reads in a sentence; "1 Mar 2025 – 9 Oct 2025" keeps its capitals.
  const windowLabel = detail.range ? detail.window_label : detail.window_label.toLowerCase();

  const rel = state.scale === "safety" ? relativeRate(state.selected) : null;

  if (rel === null || rel.reason === "no_city_exposure") {
    const headline = detail.headline ?? { incident_count: 0 };
    value.textContent = nf.format(headline.incident_count ?? 0);
    label.textContent = `reported incidents · ${windowLabel}`;
    return;
  }

  const stat = relativeStat(rel, windowLabel);
  value.textContent = stat.value;
  // stat.label/note carry the server's window_label: plain text, escaped.
  label.innerHTML = stat.note
    ? html`${stat.label}<span class="kv-note">${stat.note}</span>`
    : html`${stat.label}`;
}

async function selectCell(h3) {
  if (state.selected && state.selected !== h3) {
    map.setFeatureState({ source: "cells", id: state.selected }, { selected: false });
  }
  state.selected = h3;
  map.setFeatureState({ source: "cells", id: h3 }, { selected: true });

  const hourParam = state.hour === null ? "" : `&hour=${state.hour}`;
  const period = new URLSearchParams(periodParams());
  const [detail, ring] = await Promise.all([
    fetch(`${API}/cells/${encodeURIComponent(h3)}?${period}${hourParam}`)
      .then((r) => (r.ok ? r.json() : null)),
    fetch(`${API}/cells/ring?h3=${encodeURIComponent(h3)}&k=1&${period}&category=all`)
      .then((r) => (r.ok ? r.json() : null)),
  ]);
  if (!detail) return;
  // Kept so switching track re-reads the hourly ratings without a refetch,
  // the same way the map repaints from properties it already holds.
  state.detail = detail;

  $("detail-empty").hidden = true;
  $("detail-body").hidden = false;

  const headline = detail.headline ?? { incident_count: 0 };
  renderHeadlineStat(detail);

  // The activity tier gave up its row to the time-of-day figure; it still
  // reaches the reader through the table view.
  $("d-rank").textContent = headline.city_rank
    ? `${nf.format(headline.city_rank)} of ${nf.format(headline.city_cell_total)}`
    : "—";
  $("d-density").textContent = headline.incidents_per_km2
    ? `${nf.format(Math.round(headline.incidents_per_km2))} per km²`
    : "—";

  // Residents and workers separately, not just the sum: which of the two a
  // cell's exposure comes from is most of what distinguishes a business
  // district from a neighbourhood, and the ranking treats them alike.
  const exposure = detail.exposure;
  $("d-exposure").textContent = exposure
    ? `${nf.format(exposure.residents)} living · ${nf.format(exposure.jobs)} working`
    : "—";

  $("d-h3").textContent = h3;

  // S10: the cell plus its ring of neighbours is an O(1) H3 operation and an
  // indexed key lookup -- no spatial query anywhere in the path.
  const ringTotal = (ring?.cells ?? []).reduce((sum, c) => sum + c.incident_count, 0);
  $("d-neighbours").textContent = ring
    ? `${nf.format(ringTotal)} across ${ring.resolved} cells`
    : "—";

  renderSafety(detail.safety);
  renderHours(detail);
  renderCategoryBars(detail.by_category, detail.by_category_available);
  renderSparkline(detail.monthly, headline.window_end);
  renderOffenseMix(detail.top_offenses);
  // A later click may have moved on while this one was in flight.
  if (state.selected === h3) showCellPopup(h3, detail);
}

/**
 * Short label for a safety percentile, correct at both ends.
 *
 * The extremes need naming rather than rounding: the worst cell in a city scores
 * something like 0.0009 -- one over twice the cell count -- and "0th percentile"
 * reads as a missing value rather than as the bottom of the city. Kept terse
 * because it sits in a narrow panel column beside the tier label.
 */
function safetyLabel(percentile) {
  const value = percentile * 100;
  if (value < 1) return "bottom 1%";
  if (value > 99) return "top 1%";
  const n = Math.round(value);
  const suffix =
    n % 10 === 1 && n % 100 !== 11 ? "st"
    : n % 10 === 2 && n % 100 !== 12 ? "nd"
    : n % 10 === 3 && n % 100 !== 13 ? "rd"
    : "th";
  return `${n}${suffix} percentile`;
}

/** The percentile plus its tier label, so the number is never colour-only. */
function renderSafety(rows) {
  const byTrack = Object.fromEntries((rows ?? []).map((r) => [r.track, r]));
  const format = (row) => {
    if (!row) return "—";
    const label = SAFETY_TIER_LABELS[row.safety_tier] ?? "";
    return `${safetyLabel(row.safety_percentile)} · ${label}`;
  };
  $("d-safety-violent").innerHTML = html`${format(byTrack.violent)}`;
  $("d-safety-nonviolent").innerHTML = html`${format(byTrack.non_violent)}`;

  const missing = !rows?.length;
  $("d-safety-note").textContent = missing
    ? "The safety ranking has not been built for this city yet."
    : "Percentile against every other cell in the city, weighted by offence " +
      "severity. Higher is safer. A cell with nothing reported is shown as such " +
      "rather than as safe.";
}

/** The chart's title: the city's exact time now, "Time of Day: 10:13 PM EDT". */
function renderCityClock() {
  const popupTime = document.querySelector(".cell-popup .cp-time");
  if (popupTime) {
    popupTime.textContent = cityTimeLabel(state.cityRecord?.timezone, new Date(), { zoneName: true });
  }
  if ($("d-hour-block").hidden) return;
  const now = cityTimeLabel(state.cityRecord?.timezone, new Date(), { zoneName: true });
  $("d-hour-title").textContent = `Time of Day: ${now}`;
}

/* Keep the clock on the minute while the panel is open: first at the next
   minute boundary, then every minute. When the city's hour rolls over the
   outlook moves with it, so the whole block redraws. Skipped while the tab is
   hidden; coming back redraws at once. */
function watchCityClock() {
  let lastHour = cityHour(state.cityRecord?.timezone);
  const tick = () => {
    if (document.hidden) return;
    const hour = cityHour(state.cityRecord?.timezone);
    if (hour !== lastHour && state.detail) {
      renderHours(state.detail);
      if (state.selected) showCellPopup(state.selected, state.detail);
    } else renderCityClock();
    lastHour = hour;
  };
  setTimeout(() => {
    tick();
    setInterval(tick, 60_000);
  }, 60_000 - (Date.now() % 60_000));
  document.addEventListener("visibilitychange", tick);
}

/** "38%": the size of a fractional change, sign carried by the words around it. */
const pctOf = (change) => `${Math.abs(Math.round(change * 100))}%`;

/** "Expected to rise over the next 3 hours (+24%)": plain text. */
function outlookSentence(outlook) {
  const ahead = `over the next ${outlook.span} hours`;
  const change = outlook.change === null
    ? ""
    : ` (${outlook.change >= 0 ? "+" : "−"}${pctOf(outlook.change)})`;
  return outlook.direction === "up"
    ? `Expected to rise ${ahead}${change}`
    : outlook.direction === "down"
      ? `Expected to fall ${ahead}${change}`
      : `Expected to stay about the same ${ahead}`;
}

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

const TREND_ARROWS = { up: "↑", down: "↓", flat: "→" };

/**
 * Under the chart: which way the next three hours usually go from now in the
 * city, and the comparatively lowest-risk three-hour stretch in the next
 * twelve. Both read this cell's own hourly profile -- what these hours have
 * looked like here, not a live count -- so "expected" means "usually", and
 * the footnote says so. Returns the blocks of a window worth pointing at, for
 * the chart to tint.
 */
function renderHourOutlook(counts) {
  const box = $("d-hour-note");
  const now = cityHour(state.cityRecord?.timezone);
  const outlook = hourOutlook(counts, now);
  const win = quietestWindow(counts, now);
  if (!outlook || !win) {
    box.innerHTML = html`<p class="bar-empty">Too few incidents here with a recorded hour for an outlook.</p>`;
    return new Set();
  }
  const line = (label, arrow, text) =>
    html`<li><span class="outlook-arrow" aria-hidden="true">${arrow}</span><span><b>${label}:</b> ${text}</span></li>`;
  box.innerHTML = html`<ul>${[
    line("Next 3 hours", TREND_ARROWS[outlook.direction], outlookSentence(outlook)),
    line("Next 12 hours", "◷", windowSentence(win)),
  ]}</ul><p class="outlook-foot">From this cell's reported incidents by hour over the past
12 months — the usual pattern, not a forecast.</p>`;
  return new Set(win.startsNow || win.quieter ? win.blocks : []);
}

/**
 * The cell's day on the selected track, and its rating at the selected hour.
 *
 * The 24-bar profile is drawn whether or not an hour is selected: the shape
 * across the day is the useful thing, and it also gives the selected block
 * somewhere to sit. The rating is printed as text beside it, so it is never
 * reachable only through the colour of a hexagon.
 */
function renderHours(detail) {
  const block = $("d-hour-block");
  // The selected track's own day, so the chart and the outlook describe the
  // same offences the map and the ranking do. Hidden only when neither track
  // has an incident with a recorded hour: an empty violent day beside a busy
  // non-violent one is itself the reading.
  const profile = detail.hour_profile;
  const counts = profile?.[state.track];
  const anyHour = profile && Object.values(profile).some((day) => day.some((n) => n > 0));

  if (!counts || !anyHour) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  $("d-hour-track").textContent = `${TRACK_LABELS[state.track]} offences`;

  const max = Math.max(...counts, 1);
  const quiet = renderHourOutlook(counts);

  $("d-hours").innerHTML = html`${counts.map((n, hour) => {
    const height = Math.max((n / max) * 100, n > 0 ? 4 : 0);
    const selected = hour === state.hour ? html` data-selected` : "";
    const on = n > 0 ? html` data-on` : "";
    const lower = quiet.has(hour) ? html` data-quiet` : "";
    return html`<i style="height:${height}%"${on}${lower}${selected} title="${hourLabel(hour)}: ${nf.format(n)}"></i>`;
  })}`;

  renderCityClock();

  const safety = (detail.hour_safety ?? []).find((r) => r.track === state.track);
  const dash = "—";

  if (state.hour === null) {
    $("d-hour-safety").textContent = $("f-hour").disabled
      ? "hourly view not built for this range"
      : "pick an hour under Time of day";
    return;
  }

  // The all-hours ranking scaled by this cell's hour factor and re-ranked at
  // this hour. Tier 0 is the all-hours tier 0: nothing of this track reported
  // here at any hour.
  $("d-hour-safety").textContent = !safety
    ? dash
    : safety.safety_tier === 0
      ? safety.tier_label ?? "No reported incidents"
      : `${safetyLabel(safety.safety_percentile)} · ${safety.tier_label ?? ""}`;
}

/**
 * `available === false` means the split is not built at this cell size, which is
 * a different statement from an empty cell and must not borrow its wording. The
 * per-offence list below the chart is built at every resolution, so there is a
 * finer answer to send the reader to rather than a dead end.
 */
function renderCategoryBars(rows, available = true) {
  const container = $("d-categories");
  if (available === false) {
    container.innerHTML =
      `<p class="bar-empty">Not split by category at this cell size — a cell this ` +
      `small is empty in most single categories, so the split would be mostly ` +
      `zeroes. The reported offences listed below cover this cell.</p>`;
    return;
  }
  if (!rows?.length) {
    container.innerHTML = `<p class="bar-empty">No incidents reported in this cell.</p>`;
    return;
  }

  // Nominal categories: one hue for every bar. Bar length already encodes the
  // value, so the hue channel is not spent re-encoding it.
  const max = Math.max(...rows.map((r) => r.incident_count), 1);
  container.innerHTML = html`${rows.map((row) => {
    // The fallback is the server's raw category string: escaped by html``.
    const label = CATEGORY_LABELS[row.category] ?? row.category;
    const width = Math.max((row.incident_count / max) * 100, row.incident_count > 0 ? 1.5 : 0);
    return html`
        <div class="bar-row">
          <span class="bar-name">${label}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span>
          <span class="bar-value">${nf.format(row.incident_count)}</span>
        </div>`;
  })}`;
}

/** True when the whole calendar month is covered by data up to `anchorIso`. */
function isCompleteMonth(monthStartIso, anchorIso) {
  if (!anchorIso) return true;
  const [year, month] = monthStartIso.split("-").map(Number);
  const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const monthEnd = `${monthStartIso.slice(0, 8)}${String(lastDay).padStart(2, "0")}`;
  return monthEnd <= anchorIso;
}

function renderSparkline(monthly, anchorIso) {
  const svg = $("d-spark");
  // The trailing month is nearly always partial -- the source has only
  // published a few days of it -- and plotting it makes a data boundary look
  // like a collapse in reported crime. Complete months only.
  const series = (monthly ?? [])
    .filter((row) => row.category === "all")
    .filter((row) => isCompleteMonth(row.month_start, anchorIso))
    .sort((a, b) => a.month_start.localeCompare(b.month_start));

  svg.innerHTML = "";
  $("d-spark-readout").textContent = "";

  if (series.length < 2) {
    $("d-spark-from").textContent = "";
    $("d-spark-to").textContent = "";
    $("d-spark-readout").textContent = "Not enough complete months to plot a trend.";
    return;
  }

  const width = svg.clientWidth || 320;
  const height = 58;
  const padY = 6;
  const max = Math.max(...series.map((d) => d.incident_count), 1);
  const stepX = width / (series.length - 1);
  const y = (v) => height - padY - (v / max) * (height - padY * 2);
  const points = series.map((d, i) => [i * stepX, y(d.incident_count)]);

  const ns = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => {
    const el = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  };

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");

  // Recessive hairline baseline -- solid, one shade off the surface, never dashed.
  svg.appendChild(
    make("line", {
      x1: 0, y1: height - padY, x2: width, y2: height - padY,
      stroke: BASELINE, "stroke-width": 1,
    })
  );

  svg.appendChild(
    make("polyline", {
      points: points.map(([px, py]) => `${px},${py}`).join(" "),
      fill: "none",
      stroke: SERIES_1,
      "stroke-width": 2,
      "stroke-linejoin": "round",
      "stroke-linecap": "round",
    })
  );

  const marker = make("circle", { r: 3.5, fill: SERIES_1, cx: points.at(-1)[0], cy: points.at(-1)[1] });
  svg.appendChild(marker);

  const crosshair = make("line", {
    y1: 0, y2: height, stroke: INK_SECONDARY, "stroke-width": 1, opacity: "0",
  });
  svg.appendChild(crosshair);

  const monthLabel = (iso) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { month: "short", year: "numeric" });

  $("d-spark-from").textContent = monthLabel(series[0].month_start);
  $("d-spark-to").textContent = monthLabel(series.at(-1).month_start);
  // The trend series always spans the full retained history, which is wider
  // than the window driving the map -- say so rather than letting the panel
  // imply both numbers cover the same period.
  $("d-trend-title").textContent = `Monthly trend · ${series.length} complete months`;

  // The readout is always visible, so a value is never reachable only by hover.
  const readout = (index) => {
    const row = series[index];
    $("d-spark-readout").textContent =
      `${monthLabel(row.month_start)}: ${nf.format(row.incident_count)} reported`;
  };
  readout(series.length - 1);

  svg.onpointermove = (event) => {
    const box = svg.getBoundingClientRect();
    const ratio = (event.clientX - box.left) / box.width;
    const index = Math.min(series.length - 1, Math.max(0, Math.round(ratio * (series.length - 1))));
    crosshair.setAttribute("x1", points[index][0]);
    crosshair.setAttribute("x2", points[index][0]);
    crosshair.setAttribute("opacity", "0.5");
    marker.setAttribute("cx", points[index][0]);
    marker.setAttribute("cy", points[index][1]);
    readout(index);
  };
  svg.onpointerleave = () => {
    crosshair.setAttribute("opacity", "0");
    marker.setAttribute("cx", points.at(-1)[0]);
    marker.setAttribute("cy", points.at(-1)[1]);
    readout(series.length - 1);
  };
}

function renderOffenseMix(rows) {
  const body = $("d-offenses").querySelector("tbody");
  if (!rows?.length) {
    body.innerHTML = `<tr><td colspan="3">Nothing reported in this window.</td></tr>`;
    return;
  }
  // Third-party offence descriptions, verbatim from the source: built as DOM
  // nodes with textContent, so nothing in them is ever parsed as markup.
  body.replaceChildren(
    ...rows.map((row) => {
      const offence = document.createElement("td");
      offence.textContent = row.raw_offense_text;
      const nibrs = document.createElement("td");
      nibrs.className = "nibrs";
      nibrs.textContent = row.nibrs_code ?? "—";
      const count = document.createElement("td");
      count.className = "num";
      count.textContent = nf.format(row.incident_count);
      const tr = document.createElement("tr");
      tr.append(offence, nibrs, count);
      return tr;
    })
  );
}

function closeDetail() {
  removeCellPopup();
  if (state.selected) {
    map.setFeatureState({ source: "cells", id: state.selected }, { selected: false });
    state.selected = null;
  }
  state.detail = null;
  $("detail-body").hidden = true;
  $("detail-empty").hidden = false;
}

/* ------------------------------------------------------- cell size by zoom */

/* Testing UI: there is no cell-size control, so the size follows the zoom.
   Resolution 8 (~500 m cells) until the scale bar would stop reading "1 km",
   resolution 9 (~175 m) from there in. The bar shows the largest round
   distance that fits in SCALE_MAX_WIDTH pixels, so "1 km" goes exactly when
   that width spans less than a kilometre; this measures it the same way
   MapLibre's ScaleControl does -- across the middle of the map -- so the cells
   change at the same moment the bar does. */
const SCALE_MAX_WIDTH = 110;
const FINE_RES_BELOW_METERS = 1000;

function resForZoom() {
  const y = map.getContainer().clientHeight / 2;
  const meters = map.unproject([0, y]).distanceTo(map.unproject([SCALE_MAX_WIDTH, y]));
  return meters < FINE_RES_BELOW_METERS ? 9 : 8;
}

/** The same steps as the (hidden) cell-size control, plus keeping the open
    cell: the popup moves to the cell at the same spot on the new layer
    rather than closing under the user's zoom. */
async function syncResToZoom() {
  if (!state.cityRecord) return;
  const res = resForZoom();
  if (res === state.res) return;

  state.res = res;
  $("f-res").value = String(res);
  syncActivityScope();
  syncHourAvailability();
  syncSafetyAvailability();

  const spot = state.selected ? h3.cellToLatLng(state.selected) : null;
  closeDetail();
  await loadLayer();
  // A later zoom may have moved the resolution on while this layer loaded.
  if (spot && state.res === res && !state.selected) {
    selectCell(h3.latLngToCell(spot[0], spot[1], res));
  }
}

/* ---------------------------------------------------------------- cell popup */

/* The testing UI's whole cell view: the relative risk rating and the day's
   shape, in a small box on the map rather than the side panel (which is still
   rendered, hidden, so the rest of the app keeps reading from it). */
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
  const ratio = props ? cellRatio(props, paintedMeasure(), state.rampDomain) : null;

  // The rating is the same multiple of the city average the cell's colour
  // (and so the outline) comes from: the smoothed, severity-weighted rate the
  // ranking is built on. The side panel's headline uses the raw rate instead,
  // which can read very differently for a small cell.
  const windowLabel = detail.range ? detail.window_label : detail.window_label.toLowerCase();
  const track = TRACK_LABELS[state.track];
  const sense = ratio === null ? ""
    : ratio > 1.05 ? "higher than the city average"
    : ratio < 0.95 ? "lower than the city average"
    : "about the city average";
  const rating = ratio === null
    ? {
        value: "—",
        label: `no ${track} risk rating for this cell · ${windowLabel}`,
      }
    : {
        value: formatRatio(ratio),
        label: `relative ${track} risk — ${sense} · ${windowLabel}`,
      };

  const counts = detail.hour_profile?.[state.track];
  const max = Math.max(...(counts ?? []), 1);
  const now = cityHour(state.cityRecord?.timezone);
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
    ? html`<div class="cp-sub cp-time">${cityTimeLabel(state.cityRecord?.timezone, new Date(), { zoneName: true })}</div>
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

  const box = document.createElement("div");
  box.className = "cp";
  box.innerHTML = html`<div class="cp-value">${rating.value}</div>
    <div class="cp-label">${rating.label}</div>${chart}`;

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

/* ---------------------------------------------------------------- table view */

function renderTable() {
  const body = $("table-data").querySelector("tbody");
  const rows = [...state.features]
    .sort((a, b) => b.properties.count - a.properties.count)
    .slice(0, 250);

  $("table-caption").textContent =
    `Cells by reported incident count — ${state.features.length} cells, ` +
    `showing the top ${rows.length}` +
    (state.hour === null ? "" : ` · ratings at ${hourLabel(state.hour)}`);

  const props = TRACK_PROPS[state.track];
  $("th-hour-safety").textContent =
    state.hour === null ? "At hour" : `At ${hourLabel(state.hour)}`;

  const pct = (value) =>
    value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;

  body.innerHTML = html`${rows.map((feature, index) => {
    const p = feature.properties;
    return html`
      <tr>
        <td class="num">${index + 1}</td>
        <td class="mono">${p.h3}</td>
        <td class="num">${nf.format(p.count)}</td>
        <td class="num">${nf.format(p.per_km2)}</td>
        <td class="num">${(p.percentile * 100).toFixed(1)}%</td>
        <td>${TIER_LABELS[p.tier]}</td>
        <td class="num">${pct(p.safety_violent)}</td>
        <td class="num">${pct(p.safety_nonviolent)}</td>
        <td class="num">${pct(p[props.hpct])}</td>
      </tr>`;
  })}`;
}

/* --------------------------------------------------------------- methodology */

async function openMethodology() {
  const dialog = $("methodology");
  dialog.showModal();
  const response = await fetch(`${API}/methodology?city=${encodeURIComponent(state.city)}`);
  if (!response.ok) return;
  const m = await response.json();
  // Every field is server prose (registry text included): html`` escapes it.
  // terms_url becomes a link only if it is a plain http(s) URL.
  const terms = safeHttpUrl(m.terms_url);

  $("methodology-body").innerHTML = html`
    <h2>How to read this map</h2>
    <p>${m.what_this_shows}</p>

    <div class="callout">
      <h3 style="margin-top:0">What this is not</h3>
      <ul>${m.what_this_is_not.map((line) => html`<li>${line}</li>`)}</ul>
    </div>

    <h3>Known limitations</h3>
    <ul>${m.known_limitations.map((line) => html`<li>${line}</li>`)}</ul>

    <h3>The cell model</h3>
    <p>
      Cells are ${m.cell_model.grid} hexagons at resolution
      ${m.cell_model.primary_resolution} (${m.cell_model.primary_resolution_note}),
      with resolution ${m.cell_model.detail_resolution}
      (${m.cell_model.detail_resolution_note}) and resolution
      ${m.cell_model.fine_resolution} available as drill-downs.
    </p>
    <p>${m.cell_model.fine_resolution_note}</p>
    <p>${m.cell_model.relative_measure}</p>

    ${m.safety_measure ? html`
    <h3>The safety ranking</h3>
    <p>${m.safety_measure.what_it_is}</p>
    <p>${m.safety_measure.why_two_rankings}</p>
    <p>${m.safety_measure.weights.note}</p>
    <dl>
      <div><dt>Severity source</dt><dd>${m.safety_measure.weights.source ?? "—"}</dd></div>
      <div><dt>Weight scheme</dt><dd>${m.safety_measure.weights.scheme_version ?? "—"}</dd></div>
      <div><dt>Published weights cover</dt><dd>${
        m.safety_measure.weights.published_share === null ||
        m.safety_measure.weights.published_share === undefined
          ? "—"
          : `${(m.safety_measure.weights.published_share * 100).toFixed(1)}% of incidents`
      }</dd></div>
    </dl>
    <p>${m.safety_measure.weights.fallback_note}</p>
    <p>${m.safety_measure.smoothing.note}</p>
    <ul>${m.safety_measure.known_limitations.map((line) => html`<li>${line}</li>`)}</ul>
    ` : ""}

    ${m.time_of_day ? html`
    <h3>Time of day</h3>
    <p>${m.time_of_day.what_it_is}</p>
    <ul>${m.time_of_day.how_it_works.map((line) => html`<li>${line}</li>`)}</ul>
    <p>${m.time_of_day.hour_factor_note}</p>

    <div class="callout">
      <h3 style="margin-top:0">What the timestamps are</h3>
      <p style="margin-bottom:0">${m.time_of_day.timestamp_caveat}</p>
    </div>

    <dl>
      <div><dt>Incidents with a known hour</dt><dd>${
        m.time_of_day.coverage.hour_known_share === null ||
        m.time_of_day.coverage.hour_known_share === undefined
          ? "—"
          : `${(m.time_of_day.coverage.hour_known_share * 100).toFixed(1)}%`
      }</dd></div>
      <div><dt>Built for cell sizes</dt><dd>H3 res ${m.time_of_day.scope.resolutions.join(", ")}</dd></div>
      <div><dt>Built for windows</dt><dd>${m.time_of_day.scope.windows.join(", ")}</dd></div>
    </dl>
    <p>${m.time_of_day.coverage.note}</p>
    <p>${m.time_of_day.scope.note}</p>
    <ul>${m.time_of_day.known_limitations.map((line) => html`<li>${line}</li>`)}</ul>
    ` : ""}

    <h3>Offence classification</h3>
    <p>${m.classification.standard}</p>
    <dl>
      <div><dt>Crosswalk version</dt><dd>${m.classification.crosswalk_version}</dd></div>
      <div><dt>Raw source codes kept</dt><dd>Yes</dd></div>
      <div><dt>Demographic overlays</dt><dd>None</dd></div>
    </dl>

    <h3>Source &amp; attribution</h3>
    <p>${m.attribution}</p>
    <dl>
      <div><dt>Data as of</dt><dd>${new Date(m.data_as_of).toLocaleString()}</dd></div>
      <div><dt>Coverage</dt><dd>${m.coverage.start} to ${m.coverage.end}</dd></div>
      <div><dt>Incidents loaded</dt><dd>${nf.format(m.coverage.incidents)}</dd></div>
      <div><dt>Update cadence</dt><dd>${m.update_cadence}</dd></div>
    </dl>
    <p>${m.freshness_note ?? ""}</p>
    <p>${m.location_precision_note ?? ""}</p>
    ${terms ? html`<p><a href="${terms}" target="_blank" rel="noopener">Source dataset and terms of use</a></p>` : ""}
  `;
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

const kmLabel = (km) => (km < 10 ? `${km.toFixed(1)} km` : `${nf.format(Math.round(km))} km`);

/**
 * The city to open on, from a location the visitor has *already* allowed.
 *
 * Never prompts: a permission dialog the moment the page opens, before anyone
 * has asked for anything, is the wrong first impression for a safety map. Where
 * the browser has no grant on record this returns null at once and the page
 * opens on DEFAULT_CITY as before. Bounded by a short timeout, because it sits
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

/** selectCity, with the picker kept in step -- it only listens for user changes. */
async function switchCity(sourceId) {
  $("f-city").value = sourceId;
  await selectCity(sourceId);
}

/**
 * Take the visitor to their own cell.
 *
 * If they are standing in a different served city from the one on screen, that
 * city opens first -- declaring them "outside coverage" while they stand in
 * Chicago with Philadelphia selected was the picker's fault, not theirs.
 * Outside every city, the nearest one is named with a way to open it.
 */
async function goToLocation(point) {
  hideLocateNote();
  const home = cityAt(point);

  if (!home) {
    const near = nearestCity(point);
    renderCompass();
    if (!near) {
      showLocateNote("You are outside the cities this map covers.");
      return;
    }
    showLocateNote(
      `You are outside the cities this map covers. The nearest is ` +
        `${near.city.city_name}, about ${kmLabel(near.km)} away.`,
      near.city.source_id === state.city
        ? null
        : { label: `View ${near.city.city_name}`, run: () => switchCity(near.city.source_id) }
    );
    return;
  }

  if (home.source_id !== state.city) await switchCity(home.source_id);

  const cell = h3.latLngToCell(point.lat, point.lng, state.res);
  if (!state.featureIndex.has(cell)) {
    // Inside the box but past the edge of the data: across the city line, or
    // somewhere the source publishes nothing.
    showLocateNote(
      `You are near ${home.city_name}, but just outside the area its data covers.`
    );
    map.flyTo({ center: [point.lng, point.lat], zoom: Math.max(map.getZoom(), 12), duration: 900 });
    renderCompass();
    return;
  }
  const [lat, lng] = h3.cellToLatLng(cell);
  map.flyTo({ center: [lng, lat], zoom: Math.max(map.getZoom(), 13), duration: 900 });
  selectCell(cell);
}

function locateMe() {
  if (!navigator.geolocation) {
    showLocateNote("This browser does not expose a location.");
    return;
  }
  const button = $("btn-locate");
  button.disabled = true;
  button.textContent = "Locating…";

  navigator.geolocation.getCurrentPosition(
    (position) => {
      button.disabled = false;
      button.textContent = "Use my location";
      // S10: the cell is computed here, on the client, from GPS coordinates --
      // no server round trip and no spatial query is involved in "which cell
      // am I in". Which *city* is the same: every bounding box is already here.
      state.userLocation = {
        lat: position.coords.latitude,
        lng: position.coords.longitude,
      };
      followLocation();
      goToLocation(state.userLocation);
    },
    (error) => {
      button.disabled = false;
      button.textContent = "Use my location";
      showLocateNote(`Could not read your location: ${error.message}`);
    },
    { enableHighAccuracy: false, timeout: 10000 }
  );
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

/** The city on screen, if `point` is in it; otherwise says so and gives null.
    `what` names the point in the message ("That address"). */
function cityOnScreen(point, what) {
  const city = cityAt(point);
  if (city && city.source_id === state.city) return city;
  closeDirections();
  setStatus("search-status", `${what} is outside ${state.cityRecord?.city_name ?? "this city"}.`, true);
  return null;
}

/** The cell `point` is in, if the city's data covers it; otherwise says so
    and gives null. */
function coveredCell(point, city, what) {
  const cell = h3.latLngToCell(point.lat, point.lng, state.res);
  if (state.featureIndex.has(cell)) return cell;
  // Inside the city's box but past the edge of its data: across the city
  // line, or somewhere the source publishes nothing.
  closeDirections();
  setStatus("search-status", `${what} is just outside the area ${city.city_name}'s data covers.`, true);
  return null;
}

/** Pin `place`, fly to it and open its cell's popup -- if it is a cell of
    the city on screen. */
async function showPlace(place) {
  const seq = ++search.seq;
  clearSearchPin();
  const city = cityOnScreen(place, "That address");
  if (!city) return;
  setStatus("search-status", "");
  closeDetail();
  await flyAndSettle([place.lng, place.lat], Math.max(map.getZoom(), SEARCH_ZOOM));
  if (seq !== search.seq) return;

  const cell = coveredCell(place, city, "That address");
  if (cell) pinPlace(place, cell);
}

/** Right-click on the map: pin that point where it is, with no camera move,
    and fill the search box with the nearest address -- a way to find an
    address by pointing at it. Directions then work as for a typed one. */
async function pinClickedPoint(lngLat) {
  const point = { lat: lngLat.lat, lng: lngLat.lng };
  const seq = ++search.seq;
  clearSearchPin();
  const city = cityOnScreen(point, "That point");
  if (!city) return;
  const cell = coveredCell(point, city, "That point");
  if (!cell) return;

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

function openDirections() {
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
  $("dir-from").focus();
}

function closeDirections() {
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
  const preset = currentPreset();
  if (preset) request.window = preset.id;

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
  dir.selected = result.picks?.[state.track]?.fastest ?? 0;
  renderDirections();
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
    (result.hourly ? ", at the hour each stretch would be passed." : ", all hours.");

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

  $("dir-why").textContent = pickReason(result.picks?.[state.track], result.mode) ?? "";
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
    // Placeholder only: loadFreshness fits the real bounds from the city's own
    // snapshot before the first paint, at duration 0, so this is never seen.
    center: [-98.5, 39.5],
    zoom: 3,
    // No minZoom. With six cities spread across the country, a floor tight
    // enough for one city is a floor that cannot show another -- and fitBounds
    // on Los Angeles needs to go wider than a Philadelphia-shaped limit allows.
    maxZoom: 17,
    attributionControl: { compact: true },
  });

  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ maxWidth: SCALE_MAX_WIDTH, unit: "metric" }), "bottom-right");

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
    paint: {
      "fill-color": ZERO_FILL,
      "fill-opacity": [
        "case",
        ["boolean", ["feature-state", "selected"], false], 0.95,
        ["boolean", ["feature-state", "hover"], false], 0.9,
        0.74,
      ],
    },
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
    if (!$("search-field").hidden) pinClickedPoint(event.lngLat);
  });

  // The compass reads from the map centre until a location is shared, and its
  // rose turns with the map either way.
  map.on("moveend", renderCompass);
  // Kept, so a search can wait for the layer its own zoom set loading.
  map.on("zoomend", () => {
    resSync = syncResToZoom();
  });
  map.on("rotate", () => {
    $("compass-rose").style.transform = `rotate(${-map.getBearing()}deg)`;
  });

  initRouteLayers();
  window.__safetyMap = map;
}

/* --------------------------------------------------------------------- wiring */

/** Recolour from data already in hand -- no refetch. */
function repaint() {
  map.setPaintProperty("cells-fill", "fill-color", fillColorExpression());
  renderCompass();
}

/** Fill the time-of-day control with the 24 hour blocks, after "All hours". */
function fillHourOptions() {
  const select = $("f-hour");
  for (let hour = 0; hour < 24; hour++) select.add(new Option(hourLabel(hour), String(hour)));
}

/** "EDT": the selected city's zone as its clocks read today; "" if unknown. */
function cityZoneName() {
  const timeZone = state.cityRecord?.timezone;
  if (!timeZone) return "";
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "short" })
      .formatToParts(new Date())
      .find((part) => part.type === "timeZoneName")?.value ?? "";
  } catch {
    return ""; // RangeError for a zone this browser does not know
  }
}

/** "11 AM – 12 PM EDT" for each hour block, in the selected city's zone. */
function labelHourOptions() {
  const zone = cityZoneName();
  for (const option of $("f-hour").options) {
    if (option.value === "all") continue;
    const hour = Number(option.value);
    option.text = `${hourName(hour)} – ${hourName(hour + 1)}${zone ? ` ${zone}` : ""}`;
  }
}

/**
 * Enable or disable the hour control for the current window and cell size.
 *
 * The pipeline builds the hourly layers only where the counts support them, so
 * the control says so up front rather than letting the request 400 or, worse,
 * return a layer of nulls that looks like "nothing happens here at 3am".
 * Clears any hour already set, since it is about to stop being served.
 */
function syncHourAvailability() {
  const ok =
    HOURLY_RESOLUTIONS.includes(state.res) && Boolean(currentPreset()?.hourly);
  const select = $("f-hour");
  labelHourOptions();
  select.disabled = !ok;
  if (!ok) state.hour = null;
  select.value = state.hour === null ? "all" : String(state.hour);

  // Explanations ride on the tooltip so nothing sits under the control.
  if (!ok) {
    select.title =
      currentPreset() || !HOURLY_RESOLUTIONS.includes(state.res)
        ? "Hourly view not built at this cell size / window — map shows all hours."
        : "Hourly view is built for the last-12-months range only — map shows all hours.";
  } else if (state.meta && !state.meta.hour_known_share) {
    // An empty layer and a quiet city look identical on the map. Say which.
    select.title = "No time-of-day data loaded — run the hourly rollup.";
  } else {
    // Whose clock: the hour blocks are the city's local time, not the visitor's.
    const city = state.cityRecord?.city_name;
    select.title = city ? `Hour blocks in ${city} local time` : "";
  }
  return ok;
}

/**
 * Gate the safety ramp on cell sizes where a population denominator exists.
 *
 * Same principle as syncHourAvailability: the control says why up front rather
 * than letting the request 400 or, worse, painting a layer of nulls that reads
 * as "everywhere here is equally safe". Falls back to the count ramp, which is
 * built at every resolution, instead of leaving the map blank.
 */
function syncSafetyAvailability() {
  const ok = SAFETY_RESOLUTIONS.includes(state.res);
  const option = $("f-scale").querySelector('option[value="safety"]');
  option.disabled = !ok;

  if (!ok && state.scale === "safety") {
    state.scale = "count";
    $("f-scale").value = "count";
    $("f-track-field").hidden = true;
  }
  $("f-scale").title = ok
    ? ""
    : "Safety ranking needs a population denominator, which this cell size is " +
      "too small to carry — showing incident count.";
  return ok;
}

/**
 * Fill the date controls from the city record (windows.js).
 *
 * Runs whenever the record arrives: on load, on a city switch, and when a
 * refresh moves the data's newest day. Settles the dates first -- a carried
 * preset becomes the new city's nearest one, a legacy ?window= link becomes its
 * window's dates, nothing at all becomes the year up to today -- then clamps them
 * to what this city holds and fills the preset list.
 */
function renderRangeControls() {
  const city = state.cityRecord;
  const windows = city?.windows ?? [];
  const today = todayIso();

  if (state.carryPreset) {
    const { id, span } = state.carryPreset;
    const chosen = pickWindow(windows, id, span, city?.default_window);
    if (chosen) state.range = presetRange(chosen, today);
    state.carryPreset = null;
  } else if (!state.range) {
    const linked = findWindow(windows, state.pendingWindow);
    // The stored year rather than a year counted back from today: the stored
    // one ends where the data does, so a computed year never matches it while
    // the city publishes with any lag -- and only the stored one carries the
    // hourly layer the time-of-day control opens on.
    const year = findWindow(windows, DEFAULT_WINDOW);
    state.range = linked
      ? presetRange(linked, today)
      : year ? presetRange(year, today) : defaultRange(today);
  }
  state.pendingWindow = null;

  // The calendar starts on the city's oldest stored day. Before the city is
  // rebuilt with the daily rollup there is no such day on the record and no
  // custom range either; the presets still reach back as far as they go.
  const min = city?.selectable_start ?? windows.at(-1)?.start ?? null;
  for (const input of [$("f-from"), $("f-to")]) {
    input.min = min ?? "";
    input.max = today;
  }

  $("f-preset").replaceChildren(
    new Option("Custom dates", "custom"),
    ...windows.map((w) => new Option(w.partial ? `${w.label} (partial)` : w.label, w.id))
  );
  // Custom dates into a city that serves none: its default preset, not whichever
  // preset the clamped dates happen to land on.
  if (!city?.custom_range_resolutions?.length && !currentPreset()) {
    const fallback =
      findWindow(windows, city?.default_window) || findWindow(windows, DEFAULT_WINDOW);
    if (fallback) state.range = presetRange(fallback, today);
  }
  setRange(clampRange(state.range, min, today));
  syncActivityScope();
}

/** Select dates: state, controls and URL together. Does not reload. */
function setRange(range) {
  state.range = range;
  $("f-from").value = range.from;
  $("f-to").value = range.to;
  const preset = currentPreset();
  $("f-preset").value = preset?.id ?? "custom";
  // A preset goes into the URL by name, not as its dates. The stored windows
  // slide forward a day with every data refresh, so dates saved from one would
  // stop matching it by the next day -- reopening the page from history would
  // then land on a custom range, which has no hourly layer. ?window= always
  // means the city's current window; only hand-picked dates are pinned.
  const url = new URL(location.href);
  if (preset) {
    url.searchParams.set("window", preset.id);
    url.searchParams.delete("from");
    url.searchParams.delete("to");
  } else {
    url.searchParams.set("from", range.from);
    url.searchParams.set("to", range.to);
    url.searchParams.delete("window");
  }
  history.replaceState(null, "", url);
}

/** Whether the server ranks custom dates at this cell size for this city. */
const customRangeServed = () =>
  (state.cityRecord?.custom_range_resolutions ?? []).includes(state.res);

/**
 * Gate the date and category controls on what is served at this cell size.
 *
 * Same principle as syncHourAvailability and syncSafetyAvailability. At
 * resolution 10 the layer exists only for the one- and two-year presets and
 * the combined category, and custom dates are not served at all (there is no
 * daily rollup that fine). Coerces the current selection rather than leaving
 * one that is about to 400 -- the city's default preset is built at every
 * resolution, so there is always something to fall back to.
 *
 * Every caller reloads the layer straight afterwards, so a coerced selection is
 * picked up by that fetch rather than needing one of its own.
 */
function syncActivityScope() {
  const city = state.cityRecord;
  const windows = city?.windows ?? [];
  const categories = activityCategories(state.res);
  const custom = customRangeServed();

  for (const option of $("f-preset").options) {
    const win = findWindow(windows, option.value);
    option.disabled = win ? !windowBuiltAt(win, state.res) : !custom;
  }
  $("f-from").disabled = !custom;
  $("f-to").disabled = !custom;
  for (const option of $("f-category").options) {
    option.disabled = categories !== null && !categories.includes(option.value);
  }

  const preset = currentPreset();
  if (state.range && (preset ? !windowBuiltAt(preset, state.res) : !custom)) {
    const fallback =
      findWindow(windows, city?.default_window) || findWindow(windows, DEFAULT_WINDOW);
    if (fallback) setRange(presetRange(fallback, todayIso()));
  }
  if (categories && !categories.includes(state.category)) {
    state.category = "all";
    $("f-category").value = state.category;
  }

  const note = rangeNote(state.range, city, state.res);
  $("f-preset").title =
    state.res === 10
      ? `Only the 12-month and 2-year presets are built for cells this small. ${note}`.trim()
      : note;
  $("f-category").title = categories
    ? "A cell this small is empty in most single categories."
    : "";
}

/** The dates changed: re-gate the controls, then redraw the map and the panel. */
function onRangeChange(range) {
  setRange(clampRange(range, $("f-from").min || null, $("f-to").max || null));
  syncActivityScope();
  syncHourAvailability();
  if (state.selected) selectCell(state.selected);
  loadLayer();
}

function wireControls() {
  $("f-city").onchange = (e) => selectCity(e.target.value);
  $("f-preset").onchange = (e) => {
    const win = findWindow(state.cityRecord?.windows, e.target.value);
    // "Custom dates" changes nothing by itself; the calendars are the control.
    if (!win) {
      $("f-from").focus();
      return;
    }
    onRangeChange(presetRange(win, todayIso()));
  };
  // `change`, not `input`: it fires once a whole date is chosen, not on every
  // keystroke of one being typed. An emptied input is ignored rather than sent.
  $("f-from").onchange = (e) => {
    if (e.target.value) onRangeChange({ from: e.target.value, to: state.range.to });
  };
  $("f-to").onchange = (e) => {
    if (e.target.value) onRangeChange({ from: state.range.from, to: e.target.value });
  };
  $("f-hour").onchange = (e) => {
    state.hour = e.target.value === "all" ? null : Number(e.target.value);
    if (state.selected) selectCell(state.selected);
    loadLayer();
  };
  $("f-category").onchange = (e) => {
    state.category = e.target.value;
    loadLayer();
  };
  $("f-res").onchange = (e) => {
    state.res = Number(e.target.value);
    // Before syncHourAvailability, which reads the preset: the new resolution
    // may have just moved it.
    syncActivityScope();
    syncHourAvailability();
    syncSafetyAvailability();
    // A res-8 index is meaningless on the res-9 layer, so drop the selection.
    closeDetail();
    loadLayer();
  };
  $("f-scale").onchange = (e) => {
    state.scale = e.target.value;
    // The track tabs only mean anything while the safety ramp is on screen.
    $("f-track-field").hidden = state.scale !== "safety";
    repaint();
    // The open cell's headline measures whatever the map is coloured by, so it
    // changes with this control -- and the detail is already in hand.
    if (state.detail) renderHeadlineStat(state.detail);
  };

  // Both tracks ride along on every feature, so switching is a repaint with no
  // request and no loading state.
  $("f-track").querySelectorAll("[data-track]").forEach((button) => {
    button.onclick = () => {
      state.track = button.dataset.track;
      $("f-track")
        .querySelectorAll("[data-track]")
        .forEach((b) => b.setAttribute("aria-selected", String(b === button)));
      repaint();
      // The panel's headline, chart and hourly rating are per track too, and
      // the detail is already in hand -- re-read it rather than re-request it.
      if (state.detail) {
        renderHeadlineStat(state.detail);
        renderHours(state.detail);
        // repaint() has just moved the city average, so the popup's rating,
        // outline and chart are all re-read for the new track.
        if (state.selected) showCellPopup(state.selected, state.detail);
      }
      // Both tracks ride on every route too.
      if (dir.result) renderDirections();
      renderTable();
    };
  });

  $("btn-locate").onclick = locateMe;
  wireSearch();
  $("locate-note-close").onclick = hideLocateNote;
  $("detail-close").onclick = closeDetail;

  const tableButton = $("btn-table");
  const toggleTable = (open) => {
    $("tableview").hidden = !open;
    tableButton.setAttribute("aria-pressed", String(open));
  };
  tableButton.onclick = () => toggleTable($("tableview").hidden);
  $("table-close").onclick = () => toggleTable(false);

  document.querySelectorAll("[data-open-methodology]").forEach((el) => {
    el.onclick = openMethodology;
  });
  document.querySelectorAll("[data-close-methodology]").forEach((el) => {
    el.onclick = () => $("methodology").close();
  });

  window.addEventListener("resize", () => {
    if (state.selected) {
      // Re-lay the sparkline against the new panel width.
      selectCell(state.selected);
    }
  });
}

/* Poll the pipeline's own refresh stamp; reload the layer when the ETL runs.
   Cheap enough to sit behind the same cache-invalidation rule the API uses. */
function watchForRefresh() {
  // Skipped while the tab is hidden: a background tab has nobody to show a
  // fresher layer to. Coming back runs a tick at once, so it catches up.
  const tick = async () => {
    if (document.hidden) return;
    try {
      // Scoped to the displayed city: a bi-weekly Los Angeles refresh is not a
      // reason to reload a Philadelphia layer that has not moved.
      const response = await fetch(`${API}/version?city=${encodeURIComponent(state.city)}`);
      if (!response.ok) return;
      const version = await response.json();
      const stamp = String(version.last_refreshed_at);
      if (state.refreshStamp && stamp !== state.refreshStamp) {
        await Promise.all([loadLayer({ quiet: true }), loadFreshness()]);
        if (state.selected) selectCell(state.selected);
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

(async function main() {
  await initMap();

  // The city has to be known before anything is fetched for it, so this is the
  // one load that is not parallel with the others.
  try {
    state.city = await loadCities();
  } catch (error) {
    console.error(error);
    $("loading").textContent =
      "No city has serving data yet. Run the pipeline for one city, then reload.";
    return;
  }

  fillHourOptions();
  wireControls();
  // Not awaited: the map does not wait on routing being up.
  loadDirectionsStatus();
  // Expose read-only state for debugging and for the smoke-test driver.
  window.__safetyState = state;
  // Sequential, not parallel: the frame and the window control read the city
  // record, so the controls sync and the layer paints after it exists.
  await loadFreshness();
  syncActivityScope();
  // Open on the hour it is now in the city -- the hour blocks are its local
  // clock (web/hours.js). syncHourAvailability falls back to all hours where
  // the opening window has no hourly layer.
  state.hour = cityHour(state.cityRecord?.timezone);
  syncHourAvailability();
  syncSafetyAvailability();
  await loadLayer();

  const version = await fetch(`${API}/version?city=${encodeURIComponent(state.city)}`)
    .then((r) => r.json())
    .catch(() => null);
  state.refreshStamp = version ? String(version.last_refreshed_at) : null;
  watchForRefresh();
  watchCityClock();
})();
