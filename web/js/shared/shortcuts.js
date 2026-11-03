// Keyboard shortcuts, and the small grey keycaps that admit they exist.
//
// One module for both halves. A combo nobody can see is a combo
// nobody presses, and a keycap printed beside a button that no key actually
// reaches is worse than no keycap at all: the matcher below and the label
// beside the button are rendered from the same string, so they cannot drift.
//
// A combo is written the way it is pressed, lowercase, joined by `+`:
// `mod+k`, `mod+shift+o`, `mod+alt+p`. `mod` is ⌘ on a Mac and Ctrl elsewhere,
// which is the one difference worth abstracting; everything else is literal.

/** ⌘ or Ctrl. `userAgentData` where it exists, because `navigator.platform` is
 *  deprecated and already lies on some iPads, which report a desktop Mac. */
export const APPLE = /mac|iphone|ipad|ipod/i.test(
  navigator.userAgentData?.platform || navigator.platform || navigator.userAgent,
);

/** Keys whose `event.key` is a glyph rather than a name, addressed by their
 *  physical position instead. Holding Option on a Mac rewrites `key` into
 *  whatever the combination *types* (⌥p is π), so any combo carrying Alt has to
 *  read `code`, and reading it for the letters costs nothing. */
const PUNCTUATION = {
  Comma: ',',
  Period: '.',
  Slash: '/',
  Minus: '-',
  Equal: '=',
  Semicolon: ';',
  Backquote: '`',
};

const physical = (event) => {
  const code = event.code ?? '';
  if (code.startsWith('Key')) return code.slice(3).toLowerCase();
  if (code.startsWith('Digit')) return code.slice(5);
  if (code in PUNCTUATION) return PUNCTUATION[code];
  return (event.key ?? '').toLowerCase();
};

const parse = (combo) => {
  const parts = combo.toLowerCase().split('+');
  return {
    mod: parts.includes('mod'),
    shift: parts.includes('shift'),
    alt: parts.includes('alt'),
    key: parts.at(-1),
  };
};

/** True when this keydown is that combo, modifiers matched exactly. Exactly,
 *  because ⌘K and ⇧⌘K are different shortcuts and a loose match would fire the
 *  first one on both. */
export function matches(event, combo) {
  const want = parse(combo);
  if (want.mod !== (event.metaKey || event.ctrlKey)) return false;
  if (want.shift !== event.shiftKey) return false;
  if (want.alt !== event.altKey) return false;
  return physical(event) === want.key;
}

const NAMED = { enter: '⏎', escape: 'Esc', backspace: '⌫', delete: 'Del', f2: 'F2', ' ': 'Space' };

/** The combo as it is printed: ⌥⌘P on a Mac, Ctrl+Alt+P everywhere else, in
 *  each platform's own modifier order. */
export function label(combo) {
  const { mod, shift, alt, key } = parse(combo);
  // A Mac keyboard has no Del: the key everyone means there, and the one the
  // handlers accept, is ⌫. Printing "Del" beside it would send people hunting
  // for a key that is not on the keyboard.
  const named = APPLE && key === 'delete' ? '⌫' : NAMED[key];
  const cap = named ?? (key.length === 1 ? key.toUpperCase() : key);
  if (APPLE) return `${alt ? '⌥' : ''}${shift ? '⇧' : ''}${mod ? '⌘' : ''}${cap}`;
  return [mod && 'Ctrl', alt && 'Alt', shift && 'Shift', cap].filter(Boolean).join('+');
}

/** A keycap element for a control that has a label to sit beside. CSS hides it
 *  wherever there is no keyboard to press, so this never has to ask. */
export function keycap(combo) {
  const node = document.createElement('kbd');
  node.className = 'kbd';
  node.textContent = label(combo);
  // Decoration for the eyes only: the button beside it already says what it
  // does, and a screen reader spelling out "⌥⌘P" helps nobody.
  node.setAttribute('aria-hidden', 'true');
  return node;
}

/** Hangs the combo off an icon-only control, where there is no room for a
 *  keycap. `data-tip` is the label; the stylesheet draws both as one tooltip on
 *  hover and on keyboard focus, and draws nothing on a touch screen. */
export function tip(el, combo) {
  if (combo) el.dataset.keys = label(combo);
  // Our tooltip and the browser's would otherwise stack on the same hover.
  if (el.title && !el.dataset.tip) el.dataset.tip = el.title;
  el.removeAttribute('title');
  return el;
}

/**
 * Bind a `{ combo: handler }` map to the document.
 *
 * Every combo here carries a modifier, which is what makes a single global
 * listener safe: nothing in this map can fire while someone is typing a `p`
 * into the composer. Escape is deliberately not handled here, it means "back
 * out of the nearest thing" and only its owner knows what that is.
 *
 * @param {Record<string, (event: KeyboardEvent) => void>} map
 */
export function bind(map) {
  const entries = Object.entries(map);
  document.addEventListener('keydown', (event) => {
    if (event.isComposing) return;
    for (const [combo, run] of entries) {
      if (!matches(event, combo)) continue;
      event.preventDefault();
      run(event);
      return;
    }
  });
}

/** The one table both shells read, so a key is chosen once.
 *
 *  Chosen around the browser rather than for elegance: ⌘⇧P and ⌘⇧N open a
 *  private window in Firefox and Edge, ⌘⇧K and ⌘⇧J open devtools, and ⌘, is a
 *  menu-bar shortcut on macOS that never reaches the page. ⌥⌘ is the family
 *  that survives all of them. */
export const KEYS = {
  sidebar: 'mod+b',
  search: 'mod+k',
  newChat: 'mod+shift+o',
  newProject: 'mod+alt+p',
  settings: 'mod+alt+s',
};
