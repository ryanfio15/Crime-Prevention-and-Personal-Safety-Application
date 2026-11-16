/* ---------------------------------------------------------------------------
   The first load's progress bar, as text (GET /api/v1/progress).

   Pure functions only, so they can be tested without a browser
   (tests/js/progress.test.mjs). app.js puts the result on the page.
   --------------------------------------------------------------------------- */

/* What each phase of a city's load is doing, in words (safety/etl/progress.py). */
const PROGRESS_PHASES = {
  fetch: "downloading incidents",
  promote: "checking and storing incidents",
  census: "loading population figures",
  gold: "building the map",
  failed: "retrying after an error",
  done: "ready",
};

/** "about 25 min left", "about 1 h 10 min left", or null with no estimate yet. */
function formatEta(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 90) return "about a minute left";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `about ${hours} h ${rest} min left` : `about ${hours} h left`;
}

/**
 * The bar's state from the endpoint's body: whether to show it, how full, the
 * headline ("Loading crime data · 3 of 6 cities ready · 42% · about 25 min
 * left") and a line naming what each unfinished city is doing.
 */
function progressView(body) {
  if (!body || !body.active) return { show: false, percent: 100, text: "", detail: "" };
  const percent = Math.max(0, Math.min(100, Number(body.percent) || 0));
  const ready = body.cities_ready ?? 0;
  const total = body.cities_total ?? (body.cities ?? []).length;
  const eta = formatEta(body.eta_seconds);
  const parts = [
    "Loading crime data",
    `${ready} of ${total} ${total === 1 ? "city" : "cities"} ready`,
    `${Math.floor(percent)}%`,
    eta ?? "estimating time left",
  ];
  const busy = (body.cities ?? [])
    .filter((c) => !c.ready)
    .map((c) => {
      const what = c.phase ? PROGRESS_PHASES[c.phase] ?? c.phase : "waiting to start";
      return `${c.name}: ${what}`;
    });
  return { show: true, percent, text: parts.join(" · "), detail: busy.join(" · ") };
}
