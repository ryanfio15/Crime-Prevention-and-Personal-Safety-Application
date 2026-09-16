/* ---------------------------------------------------------------------------
   Temporary Phase 1 front end.

   Reads only the precomputed gold-layer endpoints -- it never asks the server
   to aggregate anything (design doc S9.3). The one computation it does locally
   is H3 cell membership from GPS coordinates, which S10 points out is a pure
   function of (lat, lng, resolution) and needs no server round trip at all.
   --------------------------------------------------------------------------- */

const API = "/api/v1";
const CITY = "phl";

/* Sequential blue, low -> high, stepped for the LIGHT surface: the light end
   recedes toward the page and the dark end carries the high values. Same
   documented steps as before, ordered for this surface rather than flipped. */
const SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"];
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

/** The ramp as CSS stops, for a legend that reads as one continuous bar. */
function safetyGradient() {
  const stops = SAFETY.map(
    (hex, i) => `${hex} ${((i / (SAFETY.length - 1)) * 100).toFixed(1)}%`
  );
  return `linear-gradient(to right, ${stops.join(", ")})`;
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
   without refetching -- both ship on every feature. */
const TRACK_PROPS = {
  violent: { pct: "safety_violent", tier: "stier_violent" },
  non_violent: { pct: "safety_nonviolent", tier: "stier_nonviolent" },
};

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
  selected: null,
  hovered: null,
  meta: null,
  features: [],
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

/** n colours from the validated 6-step ramp, endpoints preserved. */
function ramp(n) {
  if (n >= SEQ.length) return SEQ.slice();
  if (n <= 1) return [SEQ[SEQ.length - 1]];
  return Array.from({ length: n }, (_, i) =>
    SEQ[Math.round((i * (SEQ.length - 1)) / (n - 1))]
  );
}

/**
 * Thresholds for the count ramp.
 *
 * The distribution is heavily skewed -- a handful of Center City cells carry an
 * order of magnitude more than the median -- so a linear ramp would render the
 * whole city as one flat colour. These are the server-computed quantiles of the
 * layer actually on screen, forced strictly increasing so the step expression
 * stays valid when a narrow filter collapses several quantiles onto one value.
 */
function countThresholds(meta) {
  const breaks = meta?.breaks ?? {};
  const max = Math.round(meta?.max_count ?? 0);
  const candidates = ["p50", "p80", "p90", "p95", "p99"]
    .map((key) => Math.round(Number(breaks[key]) || 0));

  const thresholds = [];
  let previous = 0;
  for (const value of candidates) {
    const next = Math.max(value, previous + 1);
    if (next >= max) break;
    thresholds.push(next);
    previous = next;
  }
  return thresholds;
}

function fillColorExpression() {
  if (state.scale === "safety") {
    const { pct, tier } = TRACK_PROPS[state.track];
    // Interpolated on the raw percentile rather than stepped on the tier, so
    // the map is as continuous as the statistic behind it.
    const stops = SAFETY.flatMap((hex, i) => [i / (SAFETY.length - 1), hex]);
    return [
      "case",
      // Tier 0 keeps the neutral fill rather than joining the safe end: an
      // absence of reports is not evidence of safety. `coalesce` also catches
      // the case where the safety layer has not been built, so a missing value
      // reads as "no data" instead of as the least safe colour.
      ["==", ["coalesce", ["get", tier], 0], 0], ZERO_FILL,
      ["interpolate", ["linear"], ["coalesce", ["get", pct], 0], ...stops],
    ];
  }

  if (state.scale === "tier") {
    const colors = ramp(5);
    return [
      "match",
      ["get", "tier"],
      0, ZERO_FILL,
      1, colors[0],
      2, colors[1],
      3, colors[2],
      4, colors[3],
      5, colors[4],
      ZERO_FILL,
    ];
  }

  const thresholds = countThresholds(state.meta);
  const colors = ramp(thresholds.length + 1);
  const expression = ["step", ["get", "count"], ZERO_FILL, 1, colors[0]];
  thresholds.forEach((threshold, index) => {
    expression.push(threshold, colors[index + 1]);
  });
  return expression;
}

/* ------------------------------------------------------------------- legend */

function renderLegend() {
  const legend = $("legend");
  const meta = state.meta;
  if (!meta) return;
  legend.setAttribute("aria-hidden", "false");

  if (state.scale === "safety") {
    $("legend-title").textContent =
      `Safety ranking — ${TRACK_LABELS[state.track]} offences`;
    // One gradient-filled bar, not twenty adjacent swatches: the measure is
    // continuous, so a stepped key would imply bands the data does not have.
    $("legend-ramp").classList.add("is-smooth");
    $("legend-ramp").innerHTML =
      `<span style="background:${safetyGradient()}"></span>`;
    $("legend-ticks").innerHTML =
      "<span>Least safe</span><span>Median</span><span>Safest</span>";
    $("legend-foot").innerHTML =
      `<span class="legend-zero"><i></i> Nothing of this kind reported</span>` +
      `<br>Each cell ranked against the other ${nf.format(meta.cell_count)} cells, ` +
      `weighted by offence severity. A cell with no reports is not therefore safe.`;
    return;
  }

  // Every other scale is banded, so restore the 2px gaps between swatches.
  $("legend-ramp").classList.remove("is-smooth");

  if (state.scale === "tier") {
    const colors = ramp(5);
    $("legend-title").textContent = "Relative activity within Philadelphia";
    $("legend-ramp").innerHTML = colors
      .map((c) => `<span style="background:${c}"></span>`)
      .join("");
    $("legend-ticks").innerHTML = "<span>Lowest fifth</span><span>Highest fifth</span>";
    $("legend-foot").innerHTML =
      `<span class="legend-zero"><i></i> No reported incidents</span>` +
      `<br>Each cell ranked against the other ${nf.format(meta.cell_count)} cells in the city.`;
    return;
  }

  const thresholds = countThresholds(meta);
  const colors = ramp(thresholds.length + 1);
  $("legend-title").textContent = "Reported incidents per cell";
  $("legend-ramp").innerHTML = colors
    .map((c) => `<span style="background:${c}"></span>`)
    .join("");

  const ticks = ["1", ...thresholds.map((t) => nf.format(t)), nf.format(meta.max_count)];
  // Only the ends and the midpoint are labelled; a number under every step is unreadable.
  const shown = [ticks[0], ticks[Math.floor(ticks.length / 2)], ticks[ticks.length - 1]];
  $("legend-ticks").innerHTML = shown.map((t) => `<span>${t}</span>`).join("");
  $("legend-foot").innerHTML =
    `<span class="legend-zero"><i></i> No reported incidents</span>` +
    `<br>Steps are quantiles, not equal widths &mdash; the distribution is heavily skewed.`;
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

  try {
    const response = await fetch(`${API}/cells?${params}`);
    if (!response.ok) throw new Error(`cells request failed: ${response.status}`);
    const collection = await response.json();

    state.meta = collection.metadata;
    state.features = collection.features;

    const source = map.getSource("cells");
    if (source) source.setData(collection);

    map.setPaintProperty("cells-fill", "fill-color", fillColorExpression());
    renderLegend();
    renderTable();
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

  const [detail, ring] = await Promise.all([
    fetch(`${API}/cells/${h3}?window=${state.window}`).then((r) => (r.ok ? r.json() : null)),
    fetch(`${API}/cells/ring?h3=${h3}&k=1&window=${state.window}&category=all`)
      .then((r) => (r.ok ? r.json() : null)),
  ]);
  if (!detail) return;

  $("detail-empty").hidden = true;
  $("detail-body").hidden = false;

  const headline = detail.headline ?? { incident_count: 0 };
  $("d-count").textContent = nf.format(headline.incident_count ?? 0);
  $("d-count-label").textContent =
    `reported incidents · ${detail.window_label.toLowerCase()}`;

  $("d-tier").textContent = TIER_LABELS[headline.activity_tier] ?? "—";
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
    `showing the top ${rows.length}`;

  const pct = (value) =>
    value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;

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
      (${m.cell_model.detail_resolution_note}) available as a drill-down.
    </p>
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
      "line-width": [
        "case",
        ["boolean", ["feature-state", "selected"], false], 2.2,
        ["boolean", ["feature-state", "hover"], false], 1.6,
        1.1,
      ],
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

    if (state.scale === "safety") {
      const { pct, tier } = TRACK_PROPS[state.track];
      const value = p[pct];
      tooltip.innerHTML =
        (value === null || value === undefined
          ? `<b>No ${TRACK_LABELS[state.track]} offences reported</b>`
          : `Safety: <b>${safetyLabel(value)}</b>`) +
        `<small>${SAFETY_TIER_LABELS[p[tier]] ?? "—"} · ${TRACK_LABELS[state.track]} · ` +
        `${nf.format(p.count)} reported incidents</small>`;
      return;
    }

    tooltip.innerHTML =
      `<b>${nf.format(p.count)}</b> reported incidents` +
      `<small>${TIER_LABELS[p.tier]} · ${nf.format(p.per_km2)} per km²</small>`;
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

function wireControls() {
  $("f-window").onchange = (e) => {
    state.window = e.target.value;
    if (state.selected) selectCell(state.selected);
    loadLayer();
  };
  $("f-category").onchange = (e) => {
    state.category = e.target.value;
    loadLayer();
  };
  $("f-res").onchange = (e) => {
    state.res = Number(e.target.value);
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

  // Both tracks ride along on every feature, so switching is a repaint with no
  // request and no loading state.
  $("f-track").querySelectorAll("[data-track]").forEach((button) => {
    button.onclick = () => {
      state.track = button.dataset.track;
      $("f-track")
        .querySelectorAll("[data-track]")
        .forEach((b) => b.setAttribute("aria-selected", String(b === button)));
      repaint();
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
  // Expose read-only state for debugging and for the smoke-test driver.
  window.__safetyState = state;
  await Promise.all([loadFreshness(), loadLayer()]);

  const version = await fetch(`${API}/version`).then((r) => r.json()).catch(() => null);
  state.refreshStamp = version ? String(version.last_refreshed_at) : null;
  watchForRefresh();
})();
