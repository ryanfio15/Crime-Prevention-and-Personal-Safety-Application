/* ---------------------------------------------------------------------------
   Light / dark theme, set before first paint.

   Loaded in <head> as an external script (the CSP allows no inline scripts),
   so html[data-theme] is set before the stylesheet's tokens are used and the
   page never flashes the wrong colours.

   The default follows the system (prefers-color-scheme) and keeps following
   it while the visitor has not chosen. The sun / moon switch (app.js) saves a
   choice, which then wins. Storage can throw (private windows, blocked site
   data), so every read and write is wrapped: without it, the system decides.

   A classic script, not a module, like html.js: Node's vm loads it unchanged
   in tests/js/theme.test.mjs.
   --------------------------------------------------------------------------- */
(function (root) {
  const THEME_KEY = "safety-theme";
  const THEMES = ["light", "dark"];

  function savedTheme(storage) {
    try {
      const value = storage?.getItem(THEME_KEY);
      return THEMES.includes(value) ? value : null;
    } catch {
      return null;
    }
  }

  function saveTheme(storage, theme) {
    try {
      if (THEMES.includes(theme)) storage?.setItem(THEME_KEY, theme);
      else storage?.removeItem(THEME_KEY);
      return true;
    } catch {
      return false;
    }
  }

  /** The theme to show: the saved choice, else the system's. */
  function resolveTheme(saved, systemDark) {
    if (THEMES.includes(saved)) return saved;
    return systemDark ? "dark" : "light";
  }

  root.THEME_KEY = THEME_KEY;
  root.savedTheme = savedTheme;
  root.saveTheme = saveTheme;
  root.resolveTheme = resolveTheme;

  // Below here only runs in a browser.
  if (typeof document === "undefined" || typeof window === "undefined") return;

  const storage = (() => {
    try {
      return window.localStorage;
    } catch {
      return null;
    }
  })();
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function apply(theme) {
    if (document.documentElement.dataset.theme === theme) return;
    document.documentElement.dataset.theme = theme;
    document.dispatchEvent(new CustomEvent("themechange", { detail: { theme } }));
  }

  /** The switch: save the visitor's choice and show it. */
  root.setTheme = (theme) => {
    saveTheme(storage, theme);
    apply(resolveTheme(theme, media?.matches));
  };
  root.currentTheme = () => document.documentElement.dataset.theme || "light";

  apply(resolveTheme(savedTheme(storage), media?.matches));
  // Follow the system while there is no saved choice.
  media?.addEventListener?.("change", (event) => {
    if (!savedTheme(storage)) apply(event.matches ? "dark" : "light");
  });
})(globalThis);
