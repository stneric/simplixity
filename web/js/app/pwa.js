// Service worker registration, imported by both shells.
//
// Registered from /sw.js rather than /static/sw.js: a worker's scope
// is its own directory, so one served from /static could never control the
// navigations it exists for. See the route in app/main.py.

/**
 * Mark the document as installed, for the rules the media query cannot carry.
 *
 * `display-mode: standalone` is the right question to ask and iOS answered it
 * with a lie until 17.4, which is the version this had to work below: on an
 * older iPhone the app on the home screen still matched the *browser* query, so
 * the viewport fix keyed to it never applied and the composer sat above a strip
 * of dead space where the address bar would have been. `navigator.standalone`
 * is Apple's own flag and has been right the whole time.
 */
export function markDisplayMode() {
  const installed =
    navigator.standalone === true ||
    window.matchMedia?.('(display-mode: standalone)').matches ||
    window.matchMedia?.('(display-mode: fullscreen)').matches;
  if (installed) document.documentElement.dataset.standalone = '';
}

export function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  // A worker registered over plain HTTP on a LAN address is rejected by the
  // browser rather than ignored, so the attempt is skipped where it cannot
  // work. localhost counts as secure, which keeps development honest.
  if (!window.isSecureContext) return;

  // After load: registration competes with the first paint for the same
  // connection otherwise, and nothing here is needed to render.
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Nothing to do and nothing to say. The app works without it; only
      // installability and the offline shell are lost.
    });
  });
}
