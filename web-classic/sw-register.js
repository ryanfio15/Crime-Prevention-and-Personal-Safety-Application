/* Registers the web cache (web/sw.js).

   Its own file rather than an inline <script> in index.html: the CSP in
   safety/api/security.py allows scripts from 'self' and unpkg only, with no
   'unsafe-inline', so an inline block would be blocked and never run. Kept out
   of app.js because it is infrastructure, not app behaviour.

   Registered after load so installing the worker -- which downloads the shell
   once more to store it -- never competes with the page's own first load. */
(function () {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch((err) => {
      console.warn("web cache: service worker registration failed", err);
    });
  });
})();
