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
const SURFACE_GAP = "#fcfcfb";
const SERIES_1 = "#2a78d6";
const INK_PRIMARY = "#0b0b0b";
const INK_SECONDARY = "#52514e";
const BASELINE = "#c3c2b7";

/* Safety ramp, least safe -> safest, applied continuously to the percentile.

   Built in OKLCH with lightness forced monotone from 0.38 to 0.86 across the
   twenty steps, because lightness is what keeps the ordering readable when the
   hue channel collapses: green against red is the classic red-green blindness
   failure, and a deuteranope reads this ramp as dark -> light regardless. The
   poles separate at dE 47.0 under deuteranopia and 56.9 under normal vision.

   Each arm holds its own hue and chroma is pinched almost to nothing at the
   midpoint, so the two meet through a desaturated zone rather than passing
   through yellow. Pinching matters twice over: it is the diverging
   construction, and it is what removes the visible seam where the arms meet --
   letting chroma stay up across the middle produces a green block butting
   straight into a red one, which is the opposite of a smooth ramp.

   Deep reds fall outside sRGB, so the generator gamut-maps by reducing chroma
   rather than letting channels clamp; left to clamp, the last few steps
   collapse onto the same hex exactly where the ramp matters most. All twenty
   are distinct.

   Adjacent steps are deliberately close (dL ~0.025) -- that is what makes the
   ramp read as smooth rather than banded, and it is why the categorical
   adjacent-separation gate does not apply here. The table view carries the
   exact percentile for anyone who needs to read a value rather than compare. */
const SAFETY = [
  "#810009", "#8d000b", "#99000d", "#a11518", "#a52b27",
  "#a83b35", "#aa4a42", "#aa5850", "#a8675f", "#a07671",
  "#75937a", "#6e9f78", "#6bab78", "#68b57a", "#66c07c",
  "#65ca7e", "#63d481", "#62de84", "#62e988", "#61f38b",
];

/* The incident count rides the same ramp, reversed: both views then agree that
   red is the concerning end, so switching between them is a change of measure
   and not a change of vocabulary. Quiet cells land in the green. */
const COUNT_RAMP = [...SAFETY].reverse();

/** A ramp as CSS stops, for a legend that reads as one continuous bar. */
function rampGradient(colors) {
  const stops = colors.map(
    (hex, i) => `${hex} ${((i / (colors.length - 1)) * 100).toFixed(1)}%`
  );
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

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
 * tenth and eleventh. A value scale hinges at the median, so without a stop
 * there the hinge lands inside a segment, MapLibre interpolates straight across
 * it, and the median comes out at 0.476 of the bar instead of the centre. Small
 * in pixels, but the centre of this bar is exactly the thing the scale claims
 * to mean, so it should be exact. Both the fill and the legend gradient are
 * built from this list, so they cannot drift apart.
 */
function withMidpoint(colors) {
  const half = colors.length / 2;
  return [
    ...colors.slice(0, half),
    mixHex(colors[half - 1], colors[half]),
    ...colors.slice(half),
  ];
}

/* Twenty-one steps: the ramp plus the centre the value scale hinges on. */
const VALUE_RAMP = withMidpoint(COUNT_RAMP);

/**
 * Ramp stops laid out over a measured quantity, hinged at its median.
 *
 * Colour is proportional to the value rather than to the cell's rank: the
 * bottom half of the ramp spans min..median and the top half median..max, so
 * the middle colour always falls on the median value. Cells do not divide
 * evenly either side of it -- the distribution is heavily skewed, and that
 * skew is the thing a value scale is supposed to show rather than flatten.
 *
 * Two hinged segments rather than one straight line because the skew is
 * severe: a single linear span from min to max would put the median down in
 * the first tenth of the ramp and render almost the whole city in one colour.
 */
function valueRampStops(colors, domain) {
  const { min, median, max } = domain;
  const stops = [];
  let previous = -Infinity;
  colors.forEach((hex, i) => {
    const t = i / (colors.length - 1);
    let value = t <= 0.5
      ? min + (median - min) * (t / 0.5)
      : median + (max - median) * ((t - 0.5) / 0.5);
    // MapLibre rejects an interpolate whose inputs are not strictly ascending,
    // and a sparse layer readily puts min, median and max on the same number.
    if (!(value > previous)) {
      previous = previous + Math.max(Math.abs(previous), 1) * 1e-6;
      value = previous;
    }
    previous = value;
    stops.push(value, hex);
  });
  return stops;
}

/**
 * min / median / max of `prop` across the cells this view actually paints.
 *
 * Computed here rather than served, for the same reason the hourly count rank
 * is: the features are already in the browser, and the alternative is another
 * precomputed table existing only to colour one view. Cells that render in the
 * neutral fill are excluded -- they never take a colour from the ramp, so
 * letting them set its endpoints would hand half the gradient to cells that
 * are not drawn in it.
 */
function valueDomain(prop, isPainted) {
  const values = [];
  for (const feature of state.features) {
    const p = feature.properties;
    if (!isPainted(p)) continue;
    const value = p[prop];
    if (typeof value === "number") values.push(value);
  }
  if (!values.length) return null;
  values.sort((a, b) => a - b);
  return {
    min: values[0],
    median: values[Math.floor(values.length / 2)],
    max: values[values.length - 1],
    painted: values.length,
  };
}

/**
 * Citywide reported incidents per ambient person, for the layer on screen.
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
function cityExposureRate() {
  let incidents = 0;
  let people = 0;
  for (const feature of state.features) {
    const p = feature.properties;
    if (typeof p.exposure !== "number" || p.exposure <= 0) continue;
    incidents += p.count ?? 0;
    people += p.exposure;
  }
  return people > 0 ? { incidents, people, rate: incidents / people } : null;
}

/* Below this many ambient people, a rate per person is two small numbers
   divided: a cell with nine people and three incidents comes out at twenty-four
   times the city average, which is arithmetic rather than a finding. */
const RELATIVE_MIN_EXPOSURE = 100;

/**
 * One cell's incident rate per person, against the city's own.
 *
 * Read out of `state.features` rather than out of the detail payload, because
 * the feature is what is on screen: it carries the selected offence category,
 * where the panel's headline count is always category `all`. Both sides of the
 * ratio then come from one layer and share a denominator definition -- the same
 * ambient residents-and-jobs figure the safety ranking itself divides by.
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
  const city = cityExposureRate();
  if (!city) return { reason: "no_city_exposure" };

  const p = feature.properties;
  const count = p.count ?? 0;
  const exposure = typeof p.exposure === "number" ? p.exposure : null;

  if (exposure === null || exposure <= 0) return { reason: "no_exposure", count };
  if (count === 0) return { reason: "no_incidents", count, exposure, city };
  if (exposure < RELATIVE_MIN_EXPOSURE) {
    return { reason: "thin_exposure", count, exposure, city };
  }
  const rate = count / exposure;
  return { ratio: rate / city.rate, rate, count, exposure, city };
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
 * beside it. The two rates it came from go underneath for the same reason the
 * hour share prints its counts -- a derived figure is only checkable if the
 * numbers behind it are visible.
 */
function relativeStat(rel, windowLabel) {
  switch (rel.reason) {
    case "no_incidents":
      return {
        value: "—",
        label: `nothing reported here · ${windowLabel}`,
        note:
          `0 incidents among ${nf.format(rel.exposure)} people. An absence of ` +
          `reports is not evidence of safety.`,
      };
    case "thin_exposure":
      return {
        value: "—",
        label: `too few people here to compare · ${windowLabel}`,
        note:
          `${nf.format(rel.count)} incidents among ${nf.format(rel.exposure)} ` +
          `people — a rate per person on a denominator this small would swing on ` +
          `a single report.`,
      };
    case "no_exposure":
      return {
        value: "—",
        label: "no population figure for this cell",
        note:
          `${nf.format(rel.count)} reported incidents. The comparison divides by ` +
          `ambient population, which this cell has none apportioned to it.`,
      };
    case "unavailable":
      return {
        value: "—",
        label: "not comparable in this layer",
        note: "",
      };
  }

  // Same ±5% deadband as the hour share: inside it, the honest reading is "no
  // different", not a number to two figures.
  const sense =
    rel.ratio > 1.05 ? "higher than the city average"
    : rel.ratio < 0.95 ? "lower than the city average"
    : "about the city average";

  return {
    value: formatRatio(rel.ratio),
    label: `of the city-average incident rate per person — ${sense} · ${windowLabel}`,
    note:
      `${nf.format(rel.count)} incidents among ${nf.format(rel.exposure)} people · ` +
      `${per1k(rel.rate)} vs ${per1k(rel.city.rate)} per 1,000 citywide`,
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
    delta: "hdelta_violent",
    rate: "sm_violent", hrate: "hsm_violent",
  },
  non_violent: {
    pct: "safety_nonviolent", tier: "stier_nonviolent",
    hpct: "hsafety_nonviolent", htier: "hstier_nonviolent",
    delta: "hdelta_nonviolent",
    rate: "sm_nonviolent", hrate: "hsm_nonviolent",
  },
};

/* Mirrors safety.etl.gold.HOURLY_RESOLUTIONS / HOURLY_WINDOWS. The API rejects
   anything outside this with a reason; the client knows the same bounds so it
   can disable the control up front rather than let a request fail. */
const HOURLY_RESOLUTIONS = [8, 9];
const HOURLY_WINDOWS = ["last_12m"];

/* Mirrors safety.etl.gold.SAFETY_RESOLUTIONS. The safety ranking divides by
   ambient population apportioned from census blocks, and a res-10 cell is
   smaller than a census block -- there is no population figure at that size
   that is not interpolation. Counts are still served there, so the map falls
   back to the count ramp rather than going blank. */
const SAFETY_RESOLUTIONS = [8, 9];

/* Mirrors safety.etl.gold.ACTIVITY_WINDOWS / ACTIVITY_CATEGORIES. The activity
   layer is dense -- one row per cell per window per category, since a cell with
   no reported incidents is still part of the distribution it is ranked against
   -- so resolution 10 is built for the two widest windows and the combined
   category only. At ~0.015 km² a single category over 30 days leaves nearly
   every cell on zero, tied with every other, and a percentile over a field of
   ties is not a reading. Same bounds on the server, which answers the rest with
   the reason; the client knows them so the controls can say so first. */
const ACTIVITY_WINDOWS = { 10: ["last_12m", "last_24m"] };
const ACTIVITY_CATEGORIES = { 10: ["all"] };

const activityWindows = (res) => ACTIVITY_WINDOWS[res] ?? null;
const activityCategories = (res) => ACTIVITY_CATEGORIES[res] ?? null;

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
  /* Set during boot from ?city= or the served list; never assumed. */
  city: null,
  /* The selected city's snapshot row, so the header, the frame and the
     methodology sheet all read from one place. */
  cityRecord: null,
  window: "last_12m",
  category: "all",
  res: 8,
  scale: "safety",
  track: "violent",
  // null = all hours. Otherwise the local hour block 0-23.
  hour: null,
  selected: null,
  hovered: null,
  meta: null,
  features: [],
  // The open cell's detail payload, so a track switch re-reads rather than refetches.
  detail: null,
  // Domain the ramp is currently hinged on, so the legend and the fill
  // cannot disagree about what the middle of the bar means.
  rampDomain: null,
  refreshStamp: null,
  framed: false,
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
 * Paint expression for both modes, on a value scale hinged at the median.
 *
 * Both modes now run the ramp low-value-green to high-value-red, so red is the
 * concerning end in either and switching between them is a change of measure
 * rather than a change of vocabulary. The safety mode's value is the smoothed
 * severity-weighted rate -- the quantity its ranking is built on, so colour and
 * percentile order cells identically even though only one of them is a rank.
 *
 * The chosen domain is stashed on `state` for the legend, so the key and the
 * fill can never disagree about what the middle of the bar means.
 */
function fillColorExpression() {
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

  const domain = valueDomain(valueProp, isPainted);
  state.rampDomain = domain;

  if (!domain) return ZERO_FILL;

  const guard = state.scale === "safety"
    ? ["==", ["coalesce", ["get", tier], 0], 0]
    : ["==", ["coalesce", ["get", valueProp], 0], 0];

  return [
    "case",
    guard, ZERO_FILL,
    [
      "interpolate", ["linear"],
      ["coalesce", ["get", valueProp], domain.min],
      ...valueRampStops(VALUE_RAMP, domain),
    ],
  ];
}

/* Approximate width of a cell, in metres, per resolution. */
const CELL_SPAN_M = { 8: 530, 9: 200, 10: 76 };

/**
 * Resting width of the hairline between fills.
 *
 * The surface-coloured hairline only reads as a 2px gap while a cell is more
 * than a few pixels across. At resolution 10 the whole-city view puts ~26,000
 * hexagons on screen at roughly two pixels each, and a 1.1px line is then most
 * of the cell -- the map turns into a sheet of surface colour with no data
 * visible at all. So the line fades in at the zoom where this resolution's
 * cells are actually wide enough to carry it, and is absent below that.
 *
 * Selection and hover keep a fixed width at every zoom: those are pointer
 * feedback on one cell, not a boundary between thousands.
 */
/** Web-mercator metres per pixel at zoom 0, for the current city's latitude.
 *
 * 156,543 m/px at the equator, narrowing by cos(latitude). Taken from the city
 * being viewed rather than pinned to one: across the six cities this runs from
 * ~105,000 in Seattle to ~135,000 in Austin, which moves the zoom at which a
 * hexagon becomes wide enough to outline by about a third of a zoom level.
 * Falls back to the equator figure before the first city record arrives, which
 * only matters for the first frame. */
function groundResolution() {
  const lat = state.cityRecord?.center_lat;
  if (!Number.isFinite(lat)) return 156543;
  return 156543 * Math.cos((lat * Math.PI) / 180);
}

function outlineWidthExpression() {
  const span = CELL_SPAN_M[state.res] ?? CELL_SPAN_M[8];
  // The zoom at which a cell spans roughly six pixels.
  const legible = Math.log2((6 * groundResolution()) / span);
  return [
    "case",
    ["boolean", ["feature-state", "selected"], false], 2.2,
    ["boolean", ["feature-state", "hover"], false], 1.6,
    ["interpolate", ["linear"], ["zoom"], legible - 1, 0, legible, 1.1],
  ];
}

/* ------------------------------------------------------------------- legend */

/** Tick label for a domain endpoint: as precise as the magnitude deserves. */
function formatDomainValue(value) {
  if (!Number.isFinite(value)) return "—";
  if (Number.isInteger(value)) return nf.format(value);
  if (Math.abs(value) >= 100) return nf.format(Math.round(value));
  if (Math.abs(value) >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

function renderLegend() {
  const legend = $("legend");
  const meta = state.meta;
  if (!meta) return;
  legend.setAttribute("aria-hidden", "false");

  // Both measures are continuous, so the key is one gradient-filled bar rather
  // than a row of swatches: stepped swatches would imply bands the data does
  // not have.
  $("legend-ramp").classList.add("is-smooth");

  // Appended to every title once a time is chosen, so no view can be mistaken
  // for the all-hours one.
  const atHour = state.hour === null ? "" : ` · ${hourLabel(state.hour)}`;
  const safety = state.scale === "safety";

  $("legend-title").textContent = safety
    ? `Safety ranking — ${TRACK_LABELS[state.track]} offences${atHour}`
    : `Reported incidents per cell${atHour}`;

  // Both modes run the same direction now: green at the low end of the
  // measured value, red at the high end.
  $("legend-ramp").innerHTML =
    html`<span style="background:${rampGradient(VALUE_RAMP)}"></span>`;

  const domain = state.rampDomain;
  if (!domain) {
    $("legend-ticks").innerHTML = "";
    $("legend-foot").innerHTML = safety
      ? "The safety ranking has not been built for this layer."
      : "Nothing reported in this layer.";
    return;
  }

  // The three values the ramp is hinged on, at the positions they occupy: the
  // lowest painted cell at the left edge, the median in the middle, the highest
  // at the right. The middle tick is the whole point of a value scale -- it is
  // what the centre colour means.
  $("legend-ticks").innerHTML = html`${[domain.min, domain.median, domain.max].map(
    (value) => html`<span>${formatDomainValue(value)}</span>`
  )}`;

  const skew =
    `Colour follows the value itself and the middle of the bar is the median ` +
    `of the ${nf.format(domain.painted)} cells carrying one. The distribution ` +
    `is heavily skewed, so most cells sit left of centre.`;

  // `skew` is plain text and is escaped like any other value; the markup lives
  // only in the literal parts.
  $("legend-foot").innerHTML = safety
    ? html`<span class="legend-zero"><i></i> Nothing of this kind reported</span><br>Severity-weighted offence per 1,000 residents and workers, smoothed${
        state.hour === null ? "" : html` <b>within this hour</b>`
      }. ${skew} A cell with no reports is not therefore safe.`
    : html`<span class="legend-zero"><i></i> No reported incidents</span><br>${skew}${
        state.hour === null
          ? ""
          : html`<br>Counts exclude incidents the source published with no clock time.`
      }`;
}

/* --------------------------------------------------------------- data fetch */

async function loadLayer({ quiet = false } = {}) {
  if (!quiet) {
    $("map").classList.add("is-refetching");
    $("loading").hidden = false;
  }

  // The pointer can sit still across a layer swap, so mouseleave never fires
  // and the hover outline would stay on a cell from the previous layer.
  if (state.hovered) {
    map.setFeatureState({ source: "cells", id: state.hovered }, { hover: false });
    state.hovered = null;
  }

  const params = new URLSearchParams({
    city: state.city,
    res: String(state.res),
    window: state.window,
    category: state.category,
  });
  if (state.hour !== null) params.set("hour", String(state.hour));

  try {
    const response = await fetch(`${API}/cells?${params}`);
    if (!response.ok) throw new Error(`cells request failed: ${response.status}`);
    const collection = await response.json();

    state.meta = collection.metadata;
    state.features = collection.features;

    const source = map.getSource("cells");
    if (source) source.setData(collection);

    map.setPaintProperty("cells-fill", "fill-color", fillColorExpression());
    // The cell size may have just changed, and the hairline is sized per
    // resolution -- see outlineWidthExpression.
    map.setPaintProperty("cells-outline", "line-width", outlineWidthExpression());
    renderLegend();
    renderTable();
    // The city rate the safety headline is a share of has just moved, and so has
    // the selected cell's own count if the category changed -- that control
    // reloads the layer without reopening the panel.
    if (state.detail) renderHeadlineStat(state.detail);
    // Only now is it known whether the hourly layer exists at all.
    syncHourAvailability();
  } catch (error) {
    console.error(error);
    $("loading").textContent = "Could not load cell data. Is the API running?";
    return;
  } finally {
    $("map").classList.remove("is-refetching");
    $("loading").hidden = true;
    $("loading").textContent = "Loading…";
  }
}

async function loadFreshness({ refit = false } = {}) {
  const response = await fetch(`${API}/cities/${encodeURIComponent(state.city)}`);
  if (!response.ok) return;
  const city = await response.json();
  state.cityRecord = city;

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
 * Returns the chosen source_id. `?city=` wins if it is served, then
 * DEFAULT_CITY, then whatever is first.
 */
async function loadCities() {
  const response = await fetch(`${API}/cities`);
  if (!response.ok) throw new Error(`cities request failed: ${response.status}`);
  const { cities } = await response.json();
  if (!cities.length) throw new Error("no city has serving data yet");

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
 * window, category, cell size, hour -- are not place-specific and do carry over,
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
  // The stamp is per city now, so carrying the old one across would read as "the
  // pipeline just ran" on the next poll and trigger a pointless reload. Null
  // makes the next tick record rather than compare.
  state.refreshStamp = null;

  const url = new URL(location.href);
  url.searchParams.set("city", sourceId);
  history.replaceState(null, "", url);

  await loadFreshness({ refit: true });
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
 * incidents per person as a share of the city's.
 */
function renderHeadlineStat(detail) {
  const value = $("d-count");
  const label = $("d-count-label");
  const windowLabel = detail.window_label.toLowerCase();

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
  const [detail, ring] = await Promise.all([
    fetch(`${API}/cells/${encodeURIComponent(h3)}?window=${state.window}${hourParam}`)
      .then((r) => (r.ok ? r.json() : null)),
    fetch(`${API}/cells/ring?h3=${encodeURIComponent(h3)}&k=1&window=${state.window}&category=all`)
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

/**
 * The cell's day, and both time-of-day ratings for the selected block.
 *
 * The 24-bar profile is drawn whether or not an hour is selected: the shape
 * across the day is the useful thing, and it also gives the selected block
 * somewhere to sit. Both ratings are printed as text beside it, so neither is
 * reachable only through the colour of a hexagon.
 */
/**
 * How busy this cell is at the selected hour, against its own average hour.
 *
 * 100% is an ordinary hour here; 250% is two and a half times as many reported
 * incidents as this cell averages across the 24 blocks. A ratio against the
 * cell's own day rather than against other cells, which is the only comparison
 * that answers "is this hour unusual *here*" -- and the reason it can exceed
 * 100% without limit while never going below 0.
 */
function renderHourShare(detail) {
  const value = $("d-hourshare");
  const label = $("d-hourshare-label");
  label.textContent =
    state.hour === null ? "At this hour" : `At ${hourLabel(state.hour)}`;

  if (state.hour === null) {
    value.textContent = "enter a time of day";
    return;
  }

  const rel = detail.hour_relative;
  if (!rel) {
    value.textContent = "—";
    return;
  }
  // Withheld rather than printed: one hour against a twenty-fourth of a tiny
  // total is a ratio of two very small numbers, and it would read as a
  // confident figure.
  if (!rel.enough_evidence) {
    value.textContent = rel.day_total
      ? `too few to compare (${nf.format(rel.day_total)} all day)`
      : "no incidents here with a recorded hour";
    return;
  }

  const pct = rel.percent_of_average;
  const sense =
    pct > 105 ? "busier than its average hour"
    : pct < 95 ? "quieter than its average hour"
    : "about its average hour";
  value.innerHTML = html`<b>${nf.format(pct)}%</b> — ${sense}<span class="kv-note">${nf.format(
    rel.hour_count
  )} here vs. ${rel.mean_per_hour} per hour on average</span>`;
}

function renderHours(detail) {
  renderHourShare(detail);

  const block = $("d-hour-block");
  const rows = (detail.by_hour ?? []).filter((r) => r.category === "all");

  if (!rows.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;

  const counts = Array.from({ length: 24 }, () => 0);
  for (const row of rows) counts[row.hour_block] = row.incident_count;
  const max = Math.max(...counts, 1);
  const total = counts.reduce((sum, n) => sum + n, 0);

  $("d-hours").innerHTML = html`${counts.map((n, hour) => {
    const height = Math.max((n / max) * 100, n > 0 ? 4 : 0);
    const selected = hour === state.hour ? html` data-selected` : "";
    const on = n > 0 ? html` data-on` : "";
    return html`<i style="height:${height}%"${on}${selected} title="${hourLabel(hour)}: ${nf.format(n)}"></i>`;
  })}`;

  $("d-hour-title").textContent =
    state.hour === null
      ? `Time of day · ${nf.format(total)} with a known hour`
      : `Time of day · ${hourLabel(state.hour)}`;

  const safety = (detail.hour_safety ?? []).find((r) => r.track === state.track);
  const dash = "—";

  if (state.hour === null) {
    $("d-hour-safety").textContent = dash;
    $("d-hour-delta").textContent = dash;
    $("d-hour-note").textContent =
      "Enter a time of day to rank this cell within a single hour block.";
    return;
  }

  $("d-hour-safety").textContent = safety
    ? `${safetyLabel(safety.safety_percentile)} · ${safety.tier_label ?? ""}`
    : dash;

  $("d-hour-delta").textContent = safety?.percentile_delta == null
    ? dash
    : `${safety.delta_label} (${safety.percentile_delta >= 0 ? "+" : ""}` +
      `${(safety.percentile_delta * 100).toFixed(0)} points)`;

  $("d-hour-note").textContent =
    "Ranked against other cells at this hour, then against this cell's own " +
    "all-hours rank. The city is quieter at night as a whole, which the second " +
    "figure cannot see. Times are when police were dispatched, not when an " +
    "offence occurred.";
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
  if (state.selected) {
    map.setFeatureState({ source: "cells", id: state.selected }, { selected: false });
    state.selected = null;
  }
  state.detail = null;
  $("detail-body").hidden = true;
  $("detail-empty").hidden = false;
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
  // Signed, and in points rather than percent, because it is a difference
  // between two percentiles and not a percentage of anything.
  const points = (value) =>
    value === null || value === undefined
      ? "—"
      : `${value >= 0 ? "+" : ""}${(value * 100).toFixed(0)}`;

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
        <td class="num">${points(p[props.delta])}</td>
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
    <ul>${m.time_of_day.two_ratings.map((line) => html`<li>${line}</li>`)}</ul>
    <p>${m.time_of_day.hour_index_note}</p>

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

function locateMe() {
  if (!navigator.geolocation) {
    alert("This browser does not expose a location.");
    return;
  }
  const button = $("btn-locate");
  button.disabled = true;
  button.textContent = "Locating…";

  navigator.geolocation.getCurrentPosition(
    (position) => {
      const { latitude, longitude } = position.coords;
      // S10: the cell is computed here, on the client, from GPS coordinates --
      // no server round trip and no spatial query is involved in "which cell
      // am I in".
      const cell = h3.latLngToCell(latitude, longitude, state.res);
      button.disabled = false;
      button.textContent = "Use my location";

      const known = state.features.some((f) => f.properties.h3 === cell);
      if (!known) {
        alert(
          `That location is outside the ${
            state.cityRecord?.city_name ?? "selected city"
          } coverage area.`
        );
        return;
      }
      const [lat, lng] = h3.cellToLatLng(cell);
      map.flyTo({ center: [lng, lat], zoom: Math.max(map.getZoom(), 13), duration: 900 });
      selectCell(cell);
    },
    (error) => {
      button.disabled = false;
      button.textContent = "Use my location";
      alert(`Could not read your location: ${error.message}`);
    },
    { enableHighAccuracy: false, timeout: 10000 }
  );
}

/* ----------------------------------------------------------------------- map */

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
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-right");

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
  });

  // A surface-coloured hairline between fills reads as a 2px gap, rather than
  // as a contrasting border drawn around every mark.
  map.addLayer({
    id: "cells-outline",
    type: "line",
    source: "cells",
    paint: {
      "line-color": [
        "case",
        ["boolean", ["feature-state", "selected"], false], INK_PRIMARY,
        ["boolean", ["feature-state", "hover"], false], INK_SECONDARY,
        SURFACE_GAP,
      ],
      "line-width": outlineWidthExpression(),
    },
  });

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
    const feature = event.features?.[0];
    if (feature) selectCell(feature.properties.h3);
  });

  window.__safetyMap = map;
}

/* --------------------------------------------------------------------- wiring */

/** Recolour from data already in hand -- no refetch. */
function repaint() {
  map.setPaintProperty("cells-fill", "fill-color", fillColorExpression());
  renderLegend();
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
    HOURLY_RESOLUTIONS.includes(state.res) && HOURLY_WINDOWS.includes(state.window);
  const field = $("f-hour-field");
  field.setAttribute("aria-disabled", String(!ok));
  $("f-hour").disabled = !ok;
  $("f-hour-clear").disabled = !ok;

  if (!ok && state.hour !== null) {
    state.hour = null;
    $("f-hour").value = "";
  }
  if (!ok) {
    $("f-hour-note").textContent =
      "Not built at this cell size / window — too few incidents per hour.";
  } else if (state.hour === null) {
    $("f-hour-note").textContent = "All hours";
  } else if (state.meta && !state.meta.hour_known_share) {
    // An empty layer and a quiet city look identical on the map. Say which.
    $("f-hour-note").textContent = "No time-of-day data loaded — run the hourly rollup.";
  } else {
    $("f-hour-note").textContent = `Block ${hourLabel(state.hour)}`;
  }
  return ok;
}

function setHour(hour) {
  state.hour = hour;
  syncHourAvailability();
  if (state.selected) selectCell(state.selected);
  loadLayer();
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
  $("f-scale-note").textContent = ok
    ? ""
    : "Safety ranking needs a population denominator, which this cell size is " +
      "too small to carry — showing incident count.";
  return ok;
}

/**
 * Gate the window and category controls on what is built at this cell size.
 *
 * Same principle as syncHourAvailability and syncSafetyAvailability, applied to
 * the two controls that have always been free: at resolution 10 the layer only
 * exists for the widest windows and the combined category. Coerces the current
 * selection rather than leaving one that is about to 400 -- the narrowing keeps
 * the default view (last 12 months, all incidents) at every resolution, so there
 * is always something to fall back to.
 *
 * Every caller reloads the layer straight afterwards, so a coerced selection is
 * picked up by that fetch rather than needing one of its own.
 */
function syncActivityScope() {
  const windows = activityWindows(state.res);
  const categories = activityCategories(state.res);

  for (const option of $("f-window").options) {
    option.disabled = windows !== null && !windows.includes(option.value);
  }
  for (const option of $("f-category").options) {
    option.disabled = categories !== null && !categories.includes(option.value);
  }

  if (windows && !windows.includes(state.window)) {
    state.window = "last_12m";
    $("f-window").value = state.window;
  }
  if (categories && !categories.includes(state.category)) {
    state.category = "all";
    $("f-category").value = state.category;
  }

  $("f-window-note").textContent = windows
    ? "Shorter windows leave a cell this small empty — not enough to rank."
    : "";
  $("f-category-note").textContent = categories
    ? "A cell this small is empty in most single categories."
    : "";
}

function wireControls() {
  $("f-city").onchange = (e) => selectCity(e.target.value);
  $("f-window").onchange = (e) => {
    state.window = e.target.value;
    syncHourAvailability();
    if (state.selected) selectCell(state.selected);
    loadLayer();
  };
  $("f-category").onchange = (e) => {
    state.category = e.target.value;
    loadLayer();
  };
  $("f-res").onchange = (e) => {
    state.res = Number(e.target.value);
    // Before syncHourAvailability, which reads state.window: the new resolution
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

  // Any minute within the hour selects that block. The note beside the input
  // echoes which one, so 20:45 is never ambiguous.
  $("f-hour").onchange = (e) => {
    const value = e.target.value;
    if (!value) {
      setHour(null);
      return;
    }
    setHour(Number(value.split(":")[0]));
  };
  $("f-hour-clear").onclick = () => {
    $("f-hour").value = "";
    setHour(null);
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
      // The panel's hourly ratings are per track too, and the detail is already
      // in hand -- re-read it rather than re-request it.
      if (state.detail) renderHours(state.detail);
      renderTable();
    };
  });

  $("btn-locate").onclick = locateMe;
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
  setInterval(async () => {
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
  }, 60_000);
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

  wireControls();
  syncActivityScope();
  syncHourAvailability();
  syncSafetyAvailability();
  // Expose read-only state for debugging and for the smoke-test driver.
  window.__safetyState = state;
  // Sequential, not parallel: the outline width and the frame both read the
  // city record, so the layer should paint after it exists.
  await loadFreshness();
  await loadLayer();

  const version = await fetch(`${API}/version?city=${encodeURIComponent(state.city)}`)
    .then((r) => r.json())
    .catch(() => null);
  state.refreshStamp = version ? String(version.last_refreshed_at) : null;
  watchForRefresh();
})();
