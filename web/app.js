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
  /* Set during boot from ?city= or the served list; never assumed. */
  city: null,
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
  // The open cell's detail payload, so a track switch re-reads rather than refetches.
  detail: null,
  // Domain the ramp is currently hinged on, so the legend and the fill
  // cannot disagree about what the middle of the bar means.
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

    const source = map.getSource("cells");
    if (source) source.setData(collection);

    map.setPaintProperty("cells-fill", "fill-color", fillColorExpression());
    renderLegend();
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
 * incidents per person as a share of the city's.
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
  renderSparkline(
    result.monthly,
    anchorIso,
    result.monthly_note ?? ""
  );  
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
    value.textContent = $("f-hour").disabled
      ? "hourly view not built for this range"
      : "pick an hour under Time of day";
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

/** The chart's title: the city's exact time now, "Time of Day: 10:13 PM EDT". */
function renderCityClock() {
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
    if (hour !== lastHour && state.detail) renderHours(state.detail);
    else renderCityClock();
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
    $("d-hour-safety").textContent = dash;
    $("d-hour-delta").textContent = dash;
    return;
  }

  $("d-hour-safety").textContent = safety
    ? `${safetyLabel(safety.safety_percentile)} · ${safety.tier_label ?? ""}`
    : dash;

  $("d-hour-delta").textContent = safety?.percentile_delta == null
    ? dash
    : `${safety.delta_label} (${safety.percentile_delta >= 0 ? "+" : ""}` +
      `${(safety.percentile_delta * 100).toFixed(0)} points)`;
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


function renderSparkline(monthly, anchorIso, monthlyNote = "") {
  const svg = $("d-spark");

  // The API filters monthly records to the selected Check Risk period.
  // Keep boundary months, but identify their full-calendar-month totals.
  const series = (monthly ?? [])
    .filter((row) => row.category === "all")
    .sort((a, b) => a.month_start.localeCompare(b.month_start));

  svg.innerHTML = "";
  $("d-spark-readout").textContent = "";

  const note = $("d-trend-note");
  if (note) {
    note.textContent = monthlyNote;
  }

  if (series.length < 2) {
    $("d-spark-from").textContent = "";
    $("d-spark-to").textContent = "";
    $("d-spark-readout").textContent =
      series.length === 1
        ? "Only one month is available for this period."
        : "No monthly data is available for this period.";
    $("d-trend-title").textContent = "Monthly incident trend";
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
$("d-trend-title").textContent =
  `Monthly trend · ${series.length} months`;

  // The readout is always visible, so a value is never reachable only by hover.
  const readout = (index) => {
    const row = series[index];
    const partialNote = row.partial_month
      ? " · Full calendar-month total; partly outside selected period"
      : "";

    $("d-spark-readout").textContent =
      `${monthLabel(row.month_start)}: ${nf.format(row.incident_count)} reported${partialNote}`;
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


/* =========================================================
   Check Risk: location search, preview map and cell lookup
   ========================================================= */

let riskMap = null;
let riskMarker = null;
let riskLocation = null;

// State for the Check Risk result panel.
// Relative-risk values stay null until a validated model exists.
const riskResultState = {
  status: "idle", // idle | loading | available | outside_coverage | error

  location: null,

  selection: {
    date: null,
    time: null,
    category: "all",
  },

  historical: {
    available: false,
    h3: null,
    message: null,
    data: null,
  },

  relativeRisk: {
    score: null,
    level: null,
    cityComparison: null,
    percentile: null,
    status: "not_available",
  },

  error: null,
};

function renderCheckRiskResult() {
  const panel = $("risk-result");
  const title = $("risk-result-title");
  const level = $("risk-level");
  const cityComparison = $("risk-city-comparison");
  const percentile = $("risk-percentile");
  const locationComparison = $("risk-location-comparison");

  if (!panel) return;

  panel.hidden = false;

  // Clear previous results before rendering the new state.
  if (title) title.textContent = "";
  if (level) level.textContent = "";
  if (cityComparison) cityComparison.textContent = "";
  if (percentile) percentile.textContent = "";
  if (locationComparison) locationComparison.replaceChildren();

  if (riskResultState.status === "loading") {
    if (title) title.textContent = "Checking location…";
    if (level) {
      level.textContent =
        "Looking for historical data for this location.";
    }
    return;
  }

  if (riskResultState.status === "error") {
    if (title) title.textContent = "Could not check this location";
    if (level) {
      level.textContent =
        riskResultState.error || "Please try again.";
    }
    return;
  }

  if (riskResultState.status === "outside_coverage") {
    if (title) title.textContent = "Location outside data coverage";
    if (level) {
      level.textContent =
        riskResultState.historical.message ||
        "No historical data is available for this location.";
    }
    if (locationComparison) {
      locationComparison.textContent =
        "A relative-risk estimate is not available for this location.";
    }
    return;
  }

  if (riskResultState.status !== "available") {
    if (title) title.textContent = "Check a location";
    if (level) {
      level.textContent =
        "Search for a location or use your current location to begin.";
    }
    return;
  }

  const result = riskResultState.historical.data;
  const headline = result?.headline;

  if (!headline) {
    if (title) title.textContent = "Historical data found";
    if (level) {
      level.textContent =
        "The location has data coverage, but summary statistics are unavailable.";
    }
    return;
  }

  if (title) title.textContent = "Historical activity summary";

  if (level) {
    const count = headline.incident_count;
    const period = result.window_label || "Selected period";

    level.textContent =
      `${count ?? "Unavailable"} reported incidents · ${period}`;
  }

  if (cityComparison) {
    const rank = headline.city_rank;
    const total = headline.city_cell_total;
    const density = headline.incidents_per_km2;

    const parts = [];

    if (rank != null && total != null) {
      parts.push(`Citywide cell rank: ${rank} of ${total}`);
    }

    if (density != null) {
      parts.push(
        `Historical incident density: ${Number(density).toFixed(1)} per km²`
      );
    }

    cityComparison.textContent = parts.join(" · ");
  }

  if (percentile) {
    const activityPercentile = headline.percentile;

    percentile.textContent =
      activityPercentile == null
        ? "Historical activity percentile: unavailable"
        : `Historical activity percentile: ${
            (activityPercentile * 100).toFixed(1)
          }th percentile`;

    if (result.tier_label) {
      percentile.textContent += ` · Activity tier: ${result.tier_label}`;
    }
  }

  if (!locationComparison) return;

  // Use textContent for API-supplied strings to avoid inserting HTML.
  const summary = document.createElement("div");
  const h3 = document.createElement("p");
  h3.textContent = `H3 cell: ${result.h3 || "Unavailable"}`;
  summary.appendChild(h3);

  if (headline.window_start && headline.window_end) {
    const dates = document.createElement("p");
    dates.textContent =
      `Data range: ${headline.window_start} to ${headline.window_end}`;
    summary.appendChild(dates);
  }

  // Category breakdown.
  if (Array.isArray(result.by_category) && result.by_category.length) {
    const categoryHeading = document.createElement("h4");
    categoryHeading.textContent = "Incidents by category";
    summary.appendChild(categoryHeading);

    const categoryList = document.createElement("ul");

    for (const item of result.by_category) {
      const li = document.createElement("li");
      li.textContent =
        `${item.category}: ${item.incident_count} incidents`;
      categoryList.appendChild(li);
    }

    summary.appendChild(categoryList);
  }

  // Most common reported offenses.
  if (Array.isArray(result.top_offenses) && result.top_offenses.length) {
    const offenseHeading = document.createElement("h4");
    offenseHeading.textContent = "Most reported offenses";
    summary.appendChild(offenseHeading);

    const offenseList = document.createElement("ol");

    for (const item of result.top_offenses.slice(0, 5)) {
      const li = document.createElement("li");
      li.textContent =
        `${item.raw_offense_text}: ${item.incident_count}`;
      offenseList.appendChild(li);
    }

    summary.appendChild(offenseList);
  }

  // Keep the API's separate safety metrics distinct from activity percentile.
  if (Array.isArray(result.safety) && result.safety.length) {
    const safetyHeading = document.createElement("h4");
    safetyHeading.textContent = "Historical safety comparisons";
    summary.appendChild(safetyHeading);

    const safetyList = document.createElement("ul");

    for (const item of result.safety) {
      const li = document.createElement("li");
      const rank =
        item.safety_rank != null && item.city_cell_total != null
          ? ` · rank ${item.safety_rank} of ${item.city_cell_total}`
          : "";

      li.textContent =
        `${item.track_label || item.track}: ` +
        `${item.tier_label || "Tier unavailable"}${rank}`;

      safetyList.appendChild(li);
    }

    summary.appendChild(safetyList);
  }

  const disclaimer = document.createElement("p");
  disclaimer.textContent =
    "These figures describe reported historical incidents. " +
    "They do not predict whether a crime will occur at a selected future date or time.";
  summary.appendChild(disclaimer);

  locationComparison.appendChild(summary);
}

async function initializeRiskMap() {
  if (riskMap) {
    riskMap.resize();
    return riskMap;
  }

  const container = $("risk-map");
  if (!container) {
    throw new Error("The risk map container was not found in index.html.");
  }

  riskMap = new maplibregl.Map({
    container: "risk-map",
    style: await resolveStyle(),
    center: [-75.1652, 39.9526],
    zoom: 10,
    maxZoom: 18,
    attributionControl: { compact: true },
  });

  riskMap.addControl(
    new maplibregl.NavigationControl({ showCompass: false }),
    "top-right"
  );

  await new Promise((resolve, reject) => {
    riskMap.once("load", resolve);
    riskMap.once("error", (event) => {
      reject(event.error || new Error("The risk map could not load."));
    });
  });

  return riskMap;
}

function showRiskLocation(latitude, longitude, label) {
  riskLocation = { latitude, longitude, label };

  if (riskMarker) {
    riskMarker.setLngLat([longitude, latitude]);
  } else {
    riskMarker = new maplibregl.Marker({ color: "#176b60" })
      .setLngLat([longitude, latitude]);
  }

  riskMarker.addTo(riskMap);

  riskMap.flyTo({
    center: [longitude, latitude],
    zoom: 14,
    duration: 700,
  });

  const selected = $("selected-location");
  if (selected) selected.hidden = false;

  if ($("selected-address")) {
    $("selected-address").textContent = label;
  }

  if ($("selected-coordinates")) {
    $("selected-coordinates").textContent =
      `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`;
  }

  if ($("risk-map-caption")) {
    $("risk-map-caption").textContent =
      "Location selected successfully.";
  }

  if ($("risk-location-note")) {
    $("risk-location-note").textContent = "Location selected.";
  }
}



async function searchRiskLocation(query) {
  const trimmed = query.trim();

  if (trimmed.length < 3) {
    throw new Error("Enter at least 3 characters to search.");
  }

  const cityName =
    $("f-city")?.selectedOptions?.[0]?.textContent?.trim();

  const searchQuery = cityName
    ? `${trimmed}, ${cityName}`
    : trimmed;

  const params = new URLSearchParams({
    q: searchQuery,
  });

  const response = await fetch(
    `/api/v1/locations/search?${params.toString()}`,
    { headers: { Accept: "application/json" } }
  );

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(
      data.detail || "Location search failed. Check your connection and try again."
    );
  }

  const latitude = Number(data.latitude);
  const longitude = Number(data.longitude);

  if (
    !Number.isFinite(latitude) ||
    !Number.isFinite(longitude) ||
    latitude < -90 ||
    latitude > 90 ||
    longitude < -180 ||
    longitude > 180
  ) {
    throw new Error("The location service returned invalid coordinates.");
  }

  await initializeRiskMap();
  showRiskLocation(latitude, longitude, data.label);

  return riskLocation;
}



async function locateRisk() {
  if (!navigator.geolocation) {
    throw new Error("This browser does not support location access.");
  }

  const position = await new Promise((resolve, reject) => {
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: false,
      timeout: 12000,
      maximumAge: 60000,
    });
  });

  const { latitude, longitude } = position.coords;

  await initializeRiskMap();
  showRiskLocation(
    latitude,
    longitude,
    "My current location"
  );

  if ($("risk-location")) {
    $("risk-location").value = "My current location";
  }

  return riskLocation;
}

async function checkRisk() {
  if (!riskLocation) {
    riskResultState.status = "error";
    riskResultState.error =
      "Search for a location or select Use My Location first.";

    renderCheckRiskResult();
    throw new Error(riskResultState.error);
  }

  // Record the location and selected historical-data period.
  riskResultState.location = { ...riskLocation };
  riskResultState.selection.category = state.category || "all";
  riskResultState.selection.date = state.from || null;
  riskResultState.selection.time =
    state.hour == null ? null : state.hour;

  riskResultState.status = "loading";
  riskResultState.error = null;
  riskResultState.historical = {
    available: false,
    h3: null,
    message: null,
    data: null,
  };

  renderCheckRiskResult();

  try {
    const params = new URLSearchParams({
      lat: String(riskLocation.latitude),
      lng: String(riskLocation.longitude),
      res: String(state.res),
      ...periodParams(),
    });

    const response = await fetch(
      `${API}/cells/lookup?${params}`
    );

    if (!response.ok) {
      throw new Error(
        `The historical data lookup failed (HTTP ${response.status}).`
      );
    }

    const result = await response.json();

    if (!result.in_coverage) {
      riskResultState.status = "outside_coverage";
      riskResultState.historical = {
        available: false,
        h3: result.h3 || null,
        message:
          result.message ||
          "No historical data is available for this location.",
        data: result,
      };

      renderCheckRiskResult();
      return result;
    }

    riskResultState.status = "available";
    riskResultState.historical = {
      available: true,
      h3: result.h3 || null,
      message: result.message || null,
      data: result,
    };

    // Keep the future model fields empty until they are calculated
    // by a real, validated backend implementation.
    riskResultState.relativeRisk = {
      score: null,
      level: null,
      cityComparison: null,
      percentile: null,
      status: "not_available",
    };

    renderCheckRiskResult();
    return result;
  } catch (error) {
    riskResultState.status = "error";
    riskResultState.error =
      error.message || "An unexpected error occurred.";

    renderCheckRiskResult();
    throw error;
  }
}

function wireRiskControls() {
  const searchButton = $("risk-search-button");
  const locationInput = $("risk-location");
  const locationButton = $("btn-risk-locate");
  const checkButton = $("btn-check-risk");
  const centerButton = $("risk-map-center");

  if (searchButton) {
    searchButton.addEventListener("click", async () => {
      searchButton.disabled = true;

      try {
        await searchRiskLocation(locationInput?.value || "");
      } catch (error) {
        if ($("risk-location-note")) {
          $("risk-location-note").textContent =
            error.message || "Could not find that location.";
        }
      } finally {
        searchButton.disabled = false;
      }
    });
  }

  if (locationInput) {
    locationInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        searchButton?.click();
      }
    });
  }

  if (locationButton) {
    locationButton.addEventListener("click", async () => {
      locationButton.disabled = true;

      try {
        await locateRisk();
      } catch (error) {
        if ($("risk-location-note")) {
          const messages = {
            1: "Location permission was denied. Allow access in your browser settings.",
            2: "Your current location is unavailable.",
            3: "Location lookup timed out. Try again.",
          };

          $("risk-location-note").textContent =
            messages[error.code] ||
            error.message ||
            "Could not get your location.";
        }
      } finally {
        locationButton.disabled = false;
      }
    });
  }

  if (checkButton) {
    checkButton.addEventListener("click", async () => {
      checkButton.disabled = true;

      try {
        await checkRisk();
      } catch (error) {
        if ($("risk-result")) $("risk-result").hidden = false;
        if ($("risk-result-title")) {
          $("risk-result-title").textContent = "Could not check this location";
        }
        if ($("risk-level")) {
          $("risk-level").textContent =
            error.message || "Please try again.";
        }
      } finally {
        checkButton.disabled = false;
      }
    });
  }

  if (centerButton) {
    centerButton.addEventListener("click", () => {
      if (!riskMap || !riskLocation) return;

      riskMap.flyTo({
        center: [riskLocation.longitude, riskLocation.latitude],
        zoom: 14,
        duration: 500,
      });
    });
  }

  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");

  if ($("risk-date") && !$("risk-date").value) {
    $("risk-date").value =
      `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  }

  if ($("risk-time") && !$("risk-time").value) {
    $("risk-time").value =
      `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  }
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
      // The panel's hourly ratings are per track too, and the detail is already
      // in hand -- re-read it rather than re-request it.
      if (state.detail) renderHours(state.detail);
      renderTable();
    };
    wireRiskControls();
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



/* =========================================================
   Check Risk view
   Additive UI behavior; leaves the historical map logic intact.
   ========================================================= */

(function initCheckRiskUI() {
  const start = () => {
    const riskView = document.getElementById("check-risk-view");
    const historyView = document.getElementById("explore-history-view");
    const riskTab = document.getElementById("tab-check-risk");
    const historyTab = document.getElementById("tab-explore-history");

    const checkRiskButton = document.getElementById("btn-check-risk");

    const locationInput = document.getElementById("risk-location");
    const locationButton = document.getElementById("btn-risk-locate");
    const locationStatus = document.getElementById("risk-location-note");

    const dateInput = document.getElementById("risk-date");
    const timeInput = document.getElementById("risk-time");
    const riskCategory = document.getElementById("risk-category");

    const resultStatus = document.getElementById("risk-result-title");
    const resultDetails = document.getElementById("risk-result");
    const mapCaption = document.getElementById("risk-map-caption");
    const mapPlaceholder = document.getElementById("risk-map");
        
    let riskMap = null;
    let riskMarker = null;

    async function initRiskMap() {
      const container = document.getElementById("risk-map");

      if (!container || riskMap || !window.maplibregl) {
        return;
      }

      try {
        const style = await resolveStyle();

        riskMap = new maplibregl.Map({
          container: container,
          style: style,
          center: [-75.1652, 39.9526],
          zoom: 10
        });

        riskMap.addControl(
          new maplibregl.NavigationControl(),
          "top-right"
        );

        riskMap.on("load", () => {
          riskMap.resize();
        });

        riskMap.on("error", (event) => {
          console.error("Check Risk map error:", event.error);
        });
      } catch (error) {
        console.error("Could not initialize Check Risk map:", error);
      }
    }

    
    if (
      !riskView ||
      !historyView ||
      !riskTab ||
      !historyTab ||
      !checkRiskButton
    ) {
      console.error("Check Risk UI could not initialize. Check the HTML element IDs.");
      return;
    }

    let selectedCoordinates = null;

    /* Set date and time using the user's local browser clock. */
    const pad = (value) => String(value).padStart(2, "0");

    const setDateTimeDefaults = () => {
      const now = new Date();

      if (dateInput && !dateInput.value) {
        dateInput.value = [
          now.getFullYear(),
          pad(now.getMonth() + 1),
          pad(now.getDate()),
        ].join("-");
      }

      if (timeInput && !timeInput.value) {
        timeInput.value = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
      }
    };

    /* Reuse the existing historical category options where available. */
    const populateRiskCategories = () => {
      const historyCategory = document.getElementById("f-category");

      if (!riskCategory || !historyCategory) return;

      const previousValue = riskCategory.value;

      riskCategory.replaceChildren();

      Array.from(historyCategory.options).forEach((option, index) => {
        const copy = document.createElement("option");
        copy.value = option.value;
        copy.textContent =
          index === 0 ? "All reported categories" : option.textContent;
        riskCategory.appendChild(copy);
      });

      if (
        Array.from(riskCategory.options).some(
          (option) => option.value === previousValue
        )
      ) {
        riskCategory.value = previousValue;
      }
    };

    const setActiveView = (view) => {
      const showRisk = view === "risk";

      riskView.hidden = !showRisk;
      historyView.hidden = showRisk;

      riskTab.classList.toggle("is-active", showRisk);
      historyTab.classList.toggle("is-active", !showRisk);

      riskTab.setAttribute("aria-selected", String(showRisk));
      historyTab.setAttribute("aria-selected", String(!showRisk));

      riskTab.tabIndex = showRisk ? 0 : -1;
      historyTab.tabIndex = showRisk ? -1 : 0;

      if (!showRisk) {
        /*
         * The historical map was initialized while its view may have
         * been hidden. Resize it once its container is visible.
         */
        window.setTimeout(() => {
          const historicalMap = window.__safetyMap;

          if (historicalMap && typeof historicalMap.resize === "function") {
            historicalMap.resize();
          }
        }, 0);
      }

      window.dispatchEvent(
        new CustomEvent("safety:viewchange", {
          detail: { view },
        })
      );
    };

    riskTab.addEventListener("click", () => setActiveView("risk"));
    historyTab.addEventListener("click", () => setActiveView("history"));

    /* Allow keyboard users to move between the two tabs. */
    [riskTab, historyTab].forEach((tab) => {
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
          return;
        }

        event.preventDefault();

        const nextView =
          tab === riskTab
            ? (event.key === "ArrowRight" ? historyTab : historyTab)
            : (event.key === "ArrowRight" ? riskTab : riskTab);

        nextView.focus();
        nextView.click();
      });
    });

    /* Browser geolocation: obtain coordinates, not a fake street address. */
    if (locationButton) {
      locationButton.addEventListener("click", () => {
        if (!navigator.geolocation) {
          if (locationStatus) {
            locationStatus.textContent =
              "This browser does not support location access. Enter a destination instead.";
          }
          return;
        }

        locationButton.disabled = true;
        locationButton.textContent = "Finding location…";

        if (locationStatus) {
          locationStatus.textContent =
            "Waiting for browser location permission…";
        }

        navigator.geolocation.getCurrentPosition(
          (position) => {
            const latitude = position.coords.latitude;
            const longitude = position.coords.longitude;

            selectedCoordinates = { latitude, longitude };

            if (riskMap) {
              riskMap.flyTo({
                center: [longitude, latitude],
                zoom: 14
              });

              if (riskMarker) {
                riskMarker.setLngLat([longitude, latitude]);
              } else {
                riskMarker = new maplibregl.Marker()
                  .setLngLat([longitude, latitude])
                  .addTo(riskMap);
              }
            }            

            if (locationInput) {
              locationInput.value =
                `Current location (${latitude.toFixed(5)}, ${longitude.toFixed(5)})`;
              locationInput.dataset.latitude = String(latitude);
              locationInput.dataset.longitude = String(longitude);
            }

            if (locationStatus) {
              locationStatus.textContent =
                "Coordinates received. A street address and risk estimate have not been determined.";
            }

            if (mapCaption) {
              mapCaption.textContent =
                `Selected coordinates: ${latitude.toFixed(5)}, ${longitude.toFixed(5)}.`;
            }

            if (mapPlaceholder) {
              mapPlaceholder.textContent =
                `Location coordinates received: ${latitude.toFixed(5)}, ${longitude.toFixed(5)}. A map preview is not connected yet.`;
            }

            locationButton.disabled = false;
            locationButton.innerHTML =
              '<span aria-hidden="true">◎</span> Use My Location';
          },
          (error) => {
            let message =
              "Could not determine your location. Enter a destination manually.";

            if (error.code === error.PERMISSION_DENIED) {
              message =
                "Location permission was denied. Allow location access in your browser settings or enter a destination manually.";
            } else if (error.code === error.POSITION_UNAVAILABLE) {
              message =
                "Your current location is unavailable. Check your device location settings or enter a destination manually.";
            } else if (error.code === error.TIMEOUT) {
              message =
                "Location lookup timed out. Try again or enter a destination manually.";
            }

            if (locationStatus) {
              locationStatus.textContent = message;
            }

            locationButton.disabled = false;
            locationButton.innerHTML =
              '<span aria-hidden="true">◎</span> Use My Location';
          },
          {
            enableHighAccuracy: false,
            timeout: 12000,
            maximumAge: 60000,
          }
        );
      });
    }

    /*
     * Editing the location invalidates coordinates previously obtained
     * from the browser so the form cannot accidentally mix locations.
     */
 
    if (locationInput) {
      locationInput.addEventListener("input", () => {
        selectedCoordinates = null;
        delete locationInput.dataset.latitude;
        delete locationInput.dataset.longitude;

        if (locationStatus) {
          locationStatus.textContent =
            "Enter an address, then select Search to find it.";
        }

        if (mapCaption) {
          mapCaption.textContent =
            "Search for an address to show its location on the map.";
        }
      });
    }



    checkRiskButton.addEventListener("click", async (event) => {
      event.preventDefault();

      if (!locationInput || !locationInput.value.trim()) {
        if (locationStatus) {
          locationStatus.textContent =
            "Enter a destination or select Use My Location first.";
        }
        locationInput?.focus();
        return;
      }

      if (dateInput && !dateInput.value) {
        dateInput.focus();
        return;
      }

      if (timeInput && !timeInput.value) {
        timeInput.focus();
        return;
      }

      try {
        if (locationStatus) {
          locationStatus.textContent = "Checking historical activity…";
        }

        // Reuse the existing address-search and historical-lookup flow.
        if (!riskLocation && selectedCoordinates) {
          showRiskLocation(
            selectedCoordinates.latitude,
            selectedCoordinates.longitude,
            locationInput.value.trim()
          );
        } else if (!riskLocation) {
          await searchRiskLocation(locationInput.value.trim());
        }

        const result = await checkRisk();

        if (locationStatus) {
          locationStatus.textContent = result.in_coverage
            ? "Historical activity retrieved for the selected location."
            : (result.message || "No historical data is available for this location.");
        }
      } catch (error) {
        if (locationStatus) {
          locationStatus.textContent =
            error.message || "Could not check historical activity.";
        }
      }
    });


    setDateTimeDefaults();
    populateRiskCategories();
    setActiveView("risk");
    initRiskMap();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
