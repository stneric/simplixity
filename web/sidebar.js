// Whether the sidebar is open, remembered, and applied before anything paints.
//
// Both shells own a sidebar that is a column on a desktop and a full-screen
// drawer on a phone, which is why the answer to "does it start open" cannot be
// left to a script that runs after the first frame: on a phone that showed the
// drawer, covering the whole screen, and then animated it shut on every single
// load. The bug is not the animation, it is that the state arrived late.
//
// So it arrives twice. A four-line inline script in each shell's <head> reads
// the same key this module does and puts `boot-collapsed` on <html> before the
// first paint; this module then applies the real state to `.app` and takes the
// boot class off in the same frame. The duplication is deliberate and small,
// and the stylesheet is what keeps the two in step: neither state animates
// until `.app` is marked ready, which happens a frame later.

const KEY = 'simplixity.sidebar';
const NARROW = '(max-width: 760px)';

const read = () => {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null; // private mode; the choice still holds for this session
  }
};

const write = (collapsed) => {
  try {
    localStorage.setItem(KEY, collapsed ? 'collapsed' : 'open');
  } catch {
    // Private mode, or storage full. The toggle still works for this session.
  }
};

/** On a phone the sidebar covers the whole screen, so starting open means every
 *  visit begins on the chat list rather than on the thing you came for. A
 *  choice made by hand is remembered; without one, the viewport decides. */
export const startsCollapsed = () => {
  const saved = read();
  return saved ? saved === 'collapsed' : Boolean(window.matchMedia?.(NARROW).matches);
};

/**
 * Apply the remembered state and hand back the toggle.
 *
 * @param {HTMLElement} app the `.app` shell
 * @returns {() => boolean} toggles, remembers, and returns the new state
 */
export function mountSidebar(app) {
  app.classList.toggle('collapsed', startsCollapsed());
  // The inline script's job ends here; leaving it on would fight the class.
  document.documentElement.classList.remove('boot-collapsed');
  // A frame later, so the state the page loaded in is not animated into place.
  requestAnimationFrame(() => app.classList.add('ready'));

  return () => {
    const collapsed = app.classList.toggle('collapsed');
    write(collapsed);
    return collapsed;
  };
}
