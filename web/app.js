/* ---------------------------------------------------------------------------
   Temporary Phase 1 front end.

   Reads only the precomputed gold-layer endpoints -- it never asks the server
   to aggregate anything (design doc S9.3). The one computation it does locally
   is H3 cell membership from GPS coordinates, which S10 points out is a pure
   function of (lat, lng, resolution) and needs no server round trip at all.
   --------------------------------------------------------------------------- */

const API = "/api/v1";
const CITY = "phl";

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
const HOURLY_WINDOWS = ["last_12m", "last_24m"];

/** "20:00–21:00". The last block reads 23:00–24:00, not 23:00–00:00. */
const hourLabel = (hour) =>
  `${String(hour).padStart(2, "0")}:00–${String(hour + 1).padStart(2, "0")}:00`;

/* Rating 2 in words. Same bands as safety/api/repository.py::delta_label --
   wide on purpose, because an hourly percentile is far noisier than the
   all-hours one it is compared against and narrow bands would dress that noise
   up as movement. */
const DELTA_BANDS = [
  [-0.15, "Much worse here than usual"],
  [-0.05, "Worse here than usual"],
  [0.05, "Typical for this cell"],
  [0.15, "Better here than usual"],
];

function deltaLabel(delta) {
  if (delta === null || delta === undefined) return null;
  for (const [threshold, label] of DELTA_BANDS) {
    if (delta < threshold) return label;
  }
  return "Much better here than usual";
}

const CATEGORY_LABELS = {
  violent: "Violent",
  property: "Property",
  quality_of_life: "Quality of life",
  other: "Other",
};

const state = {
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
function outlineWidthExpression() {
  const span = CELL_SPAN_M[state.res] ?? CELL_SPAN_M[8];
  // Web-mercator ground resolution at Philadelphia's latitude is about
  // 119,940 / 2^zoom metres per pixel, so this is the zoom at which a cell
  // spans roughly six pixels.
  const legible = Math.log2((6 * 119940) / span);
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
    `<span style="background:${rampGradient(VALUE_RAMP)}"></span>`;

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
  $("legend-ticks").innerHTML = [domain.min, domain.median, domain.max]
    .map((value) => `<span>${formatDomainValue(value)}</span>`)
    .join("");

  const skew =
    `Colour follows the value itself and the middle of the bar is the median ` +
    `of the ${nf.format(domain.painted)} cells carrying one. The distribution ` +
    `is heavily skewed, so most cells sit left of centre.`;

  $("legend-foot").innerHTML = safety
    ? `<span class="legend-zero"><i></i> Nothing of this kind reported</span>` +
      `<br>Severity-weighted offence per km², smoothed` +
      (state.hour === null ? "" : " <b>within this hour</b>") +
      `. ${skew} A cell with no reports is not therefore safe.`
    : `<span class="legend-zero"><i></i> No reported incidents</span>` +
      `<br>${skew}` +
      (state.hour === null
        ? ""
        : `<br>Counts exclude incidents the source published with no clock time.`);
}

/* --------------------------------------------------------------- data fetch */

async function loadLayer({ quiet = false } = {}) {
  if (!quiet) {
    $("map").classList.add("is-refetching");
    $("loading").hidden = false;
  }

  // The pointer can sit still across a layer swap, so mouseleave never fires
  // and the tooltip would keep showing a value from the previous layer.
  $("tooltip").hidden = true;
  if (state.hovered) {
    map.setFeatureState({ source: "cells", id: state.hovered }, { hover: false });
    state.hovered = null;
  }

  const params = new URLSearchParams({
    city: CITY,
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

async function loadFreshness() {
  const response = await fetch(`${API}/cities/${CITY}`);
  if (!response.ok) return;
  const city = await response.json();

  // Frame the city from its own stored bounding box rather than a hardcoded
  // centre, so a second city needs no client change (design doc S11).
  if (!state.framed && Number.isFinite(city.bbox_west)) {
    map.fitBounds(
      [
        [city.bbox_west, city.bbox_south],
        [city.bbox_east, city.bbox_north],
      ],
      { padding: { top: 28, bottom: 28, left: 28, right: 28 }, duration: 0 }
    );
    state.framed = true;
  }

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

/* --------------------------------------------------------------- cell panel */

async function selectCell(h3) {
  if (state.selected && state.selected !== h3) {
    map.setFeatureState({ source: "cells", id: state.selected }, { selected: false });
  }
  state.selected = h3;
  map.setFeatureState({ source: "cells", id: h3 }, { selected: true });

  const hourParam = state.hour === null ? "" : `&hour=${state.hour}`;
  const [detail, ring] = await Promise.all([
    fetch(`${API}/cells/${h3}?window=${state.window}${hourParam}`)
      .then((r) => (r.ok ? r.json() : null)),
    fetch(`${API}/cells/ring?h3=${h3}&k=1&window=${state.window}&category=all`)
      .then((r) => (r.ok ? r.json() : null)),
  ]);
  if (!detail) return;
  // Kept so switching track re-reads the hourly ratings without a refetch,
  // the same way the map repaints from properties it already holds.
  state.detail = detail;

  $("detail-empty").hidden = true;
  $("detail-body").hidden = false;

  const headline = detail.headline ?? { incident_count: 0 };
  $("d-count").textContent = nf.format(headline.incident_count ?? 0);
  $("d-count-label").textContent =
    `reported incidents · ${detail.window_label.toLowerCase()}`;

  // The activity tier gave up its row to the time-of-day figure; it still
  // reaches the reader through the tooltip and the table view.
  $("d-rank").textContent = headline.city_rank
    ? `${nf.format(headline.city_rank)} of ${nf.format(headline.city_cell_total)}`
    : "—";
  $("d-density").textContent = headline.incidents_per_km2
    ? `${nf.format(Math.round(headline.incidents_per_km2))} per km²`
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
  renderCategoryBars(detail.by_category);
  renderSparkline(detail.monthly, headline.window_end);
  renderOffenseMix(detail.top_offenses);
}

/**
 * Short label for a safety percentile, correct at both ends.
 *
 * The extremes need naming rather than rounding: the worst cell in Philadelphia
 * scores 0.0009, and "0th percentile" reads as a missing value rather than as
 * the bottom of the city. Kept terse because it sits in a narrow panel column
 * beside the tier label.
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
    if (!row) return "&mdash;";
    const label = SAFETY_TIER_LABELS[row.safety_tier] ?? "";
    return `${safetyLabel(row.safety_percentile)} · ${label}`;
  };
  $("d-safety-violent").innerHTML = format(byTrack.violent);
  $("d-safety-nonviolent").innerHTML = format(byTrack.non_violent);

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
  value.innerHTML =
    `<b>${nf.format(pct)}%</b> — ${sense}` +
    `<span class="kv-note">${nf.format(rel.hour_count)} here vs. ` +
    `${rel.mean_per_hour} per hour on average</span>`;
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

  $("d-hours").innerHTML = counts
    .map((n, hour) => {
      const height = Math.max((n / max) * 100, n > 0 ? 4 : 0);
      const selected = hour === state.hour ? " data-selected" : "";
      const on = n > 0 ? " data-on" : "";
      return `<i style="height:${height}%"${on}${selected} title="${hourLabel(hour)}: ${nf.format(n)}"></i>`;
    })
    .join("");

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

function renderCategoryBars(rows) {
  const container = $("d-categories");
  if (!rows?.length) {
    container.innerHTML = `<p class="bar-empty">No incidents reported in this cell.</p>`;
    return;
  }

  // Nominal categories: one hue for every bar. Bar length already encodes the
  // value, so the hue channel is not spent re-encoding it.
  const max = Math.max(...rows.map((r) => r.incident_count), 1);
  container.innerHTML = rows
    .map((row) => {
      const label = CATEGORY_LABELS[row.category] ?? row.category;
      const width = Math.max((row.incident_count / max) * 100, row.incident_count > 0 ? 1.5 : 0);
      return `
        <div class="bar-row">
          <span class="bar-name">${label}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span>
          <span class="bar-value">${nf.format(row.incident_count)}</span>
        </div>`;
    })
    .join("");
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
  body.innerHTML = rows
    .map(
      (row) => `
      <tr>
        <td>${row.raw_offense_text}</td>
        <td class="nibrs">${row.nibrs_code ?? "—"}</td>
        <td class="num">${nf.format(row.incident_count)}</td>
      </tr>`
    )
    .join("");
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

  body.innerHTML = rows
    .map((feature, index) => {
      const p = feature.properties;
      return `
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
    })
    .join("");
}

/* --------------------------------------------------------------- methodology */

async function openMethodology() {
  const dialog = $("methodology");
  dialog.showModal();
  const response = await fetch(`${API}/methodology?city=${CITY}`);
  if (!response.ok) return;
  const m = await response.json();

  $("methodology-body").innerHTML = `
    <h2>How to read this map</h2>
    <p>${m.what_this_shows}</p>

    <div class="callout">
      <h3 style="margin-top:0">What this is not</h3>
      <ul>${m.what_this_is_not.map((line) => `<li>${line}</li>`).join("")}</ul>
    </div>

    <h3>Known limitations</h3>
    <ul>${m.known_limitations.map((line) => `<li>${line}</li>`).join("")}</ul>

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

    ${m.safety_measure ? `
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
    <ul>${m.safety_measure.known_limitations.map((line) => `<li>${line}</li>`).join("")}</ul>
    ` : ""}

    ${m.time_of_day ? `
    <h3>Time of day</h3>
    <p>${m.time_of_day.what_it_is}</p>
    <ul>${m.time_of_day.two_ratings.map((line) => `<li>${line}</li>`).join("")}</ul>
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
    <ul>${m.time_of_day.known_limitations.map((line) => `<li>${line}</li>`).join("")}</ul>
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
    ${m.terms_url ? `<p><a href="${m.terms_url}" target="_blank" rel="noopener">Source dataset and terms of use</a></p>` : ""}
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
        alert("That location is outside the Philadelphia coverage area.");
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
    center: [-75.1435, 39.9855],
    zoom: 10.9,
    minZoom: 9,
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

  const tooltip = $("tooltip");

  map.on("mousemove", "cells-fill", (event) => {
    const feature = event.features?.[0];
    if (!feature) return;
    map.getCanvas().style.cursor = "pointer";

    if (state.hovered && state.hovered !== feature.id) {
      map.setFeatureState({ source: "cells", id: state.hovered }, { hover: false });
    }
    state.hovered = feature.id;
    map.setFeatureState({ source: "cells", id: feature.id }, { hover: true });

    const p = feature.properties;
    tooltip.hidden = false;
    tooltip.style.left = `${event.point.x}px`;
    tooltip.style.top = `${event.point.y}px`;

    const props = TRACK_PROPS[state.track];
    const hourly = state.hour !== null;
    // Both ratings, whenever both exist. The second is meaningless without the
    // first, and the first alone invites reading a night-time rank as an
    // absolute statement about the hour.
    const second = hourly
      ? `<small>${deltaLabel(p[props.delta]) ?? "Not ranked at this hour"}</small>`
      : "";

    if (state.scale === "safety") {
      const pct = hourly ? props.hpct : props.pct;
      const tier = hourly ? props.htier : props.tier;
      const value = p[pct];
      const reported = hourly ? p.hcount : p.count;
      tooltip.innerHTML =
        (value === null || value === undefined
          ? `<b>No ${TRACK_LABELS[state.track]} offences reported</b>`
          : `Safety: <b>${safetyLabel(value)}</b>`) +
        `<small>${SAFETY_TIER_LABELS[p[tier]] ?? "—"} · ${TRACK_LABELS[state.track]}` +
        (hourly ? ` · ${hourLabel(state.hour)}` : "") +
        ` · ${nf.format(reported ?? 0)} reported incidents</small>` +
        second;
      return;
    }

    tooltip.innerHTML =
      `<b>${nf.format((hourly ? p.hcount : p.count) ?? 0)}</b> reported incidents` +
      (hourly
        ? `<small>${hourLabel(state.hour)} · ${nf.format(p.count)} across the whole day</small>`
        : `<small>${TIER_LABELS[p.tier]} · ${nf.format(p.per_km2)} per km²</small>`);
  });

  map.on("mouseleave", "cells-fill", () => {
    map.getCanvas().style.cursor = "";
    if (state.hovered) {
      map.setFeatureState({ source: "cells", id: state.hovered }, { hover: false });
      state.hovered = null;
    }
    tooltip.hidden = true;
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

function wireControls() {
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
    syncHourAvailability();
    // A res-8 index is meaningless on the res-9 layer, so drop the selection.
    closeDetail();
    loadLayer();
  };
  $("f-scale").onchange = (e) => {
    state.scale = e.target.value;
    // The track tabs only mean anything while the safety ramp is on screen.
    $("f-track-field").hidden = state.scale !== "safety";
    repaint();
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
      const response = await fetch(`${API}/version`);
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
  wireControls();
  syncHourAvailability();
  // Expose read-only state for debugging and for the smoke-test driver.
  window.__safetyState = state;
  await Promise.all([loadFreshness(), loadLayer()]);

  const version = await fetch(`${API}/version`).then((r) => r.json()).catch(() => null);
  state.refreshStamp = version ? String(version.last_refreshed_at) : null;
  watchForRefresh();
})();
