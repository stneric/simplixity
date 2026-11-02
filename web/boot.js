// The one thing that has to be decided before the first pixel: whether the
// sidebar starts open.
//
// Not a module, and not deferred. Both shells load their real code
// as ES modules, which by definition run after the document has been parsed and
// after the browser has had its first chance to paint. That is late enough to
// be visible: on a phone the sidebar is a full-screen drawer, so every load
// showed the drawer and then animated it shut on the way to the chat. A plain
// <script src> in the <head> blocks the parser, which is exactly what is wanted
// here, and stays inside `script-src 'self'`, which is why this is a file and
// not four lines of inline script (see CONTENT_SECURITY_POLICY in app/main.py).
//
// It decides nothing on its own. It paints the answer that web/sidebar.js is
// about to give, and that module removes this class in the same frame as it
// applies the real state. The key and the media query are duplicated there; a
// synchronous script cannot import them, so they are kept in step by hand.

(function () {
  var saved = null;
  try {
    saved = localStorage.getItem('simplixity.sidebar');
  } catch (error) {
    saved = null; // private mode; the viewport decides instead
  }
  var collapsed = saved
    ? saved === 'collapsed'
    : window.matchMedia('(max-width: 760px)').matches;
  if (collapsed) document.documentElement.classList.add('boot-collapsed');
})();
