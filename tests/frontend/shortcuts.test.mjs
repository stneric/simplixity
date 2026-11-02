// The shortcut matcher and the labels printed beside the buttons.
//
// Both come out of one table, and this is what keeps them honest: a keycap
// reading ⌥⌘P beside a button that only ⌥⌘Π can reach would be worse than no
// keycap at all. The Option case below is the real one, holding Option on a
// Mac rewrites `event.key` into the glyph the combination *types*.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { installDom } from './dom.mjs';

const platform = (value) =>
  Object.defineProperty(globalThis, 'navigator', {
    value: { platform: value, userAgent: value },
    configurable: true,
  });

installDom();
platform('MacIntel');
const mac = await import('../../web/shortcuts.js');
platform('Win32');
// A query string is what buys a second instance of a module that reads the
// platform once, at import.
const pc = await import('../../web/shortcuts.js?platform=win');

/** A keydown with nothing held, overridden by whatever the case is about. */
const press = (over) => ({
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  key: '',
  code: '',
  ...over,
});

test('the same combo is printed in each platform’s own language', () => {
  assert.equal(mac.label('mod+k'), '⌘K');
  assert.equal(pc.label('mod+k'), 'Ctrl+K');
  assert.equal(mac.label('mod+shift+o'), '⇧⌘O');
  assert.equal(pc.label('mod+shift+o'), 'Ctrl+Shift+O');
  assert.equal(mac.label('mod+alt+p'), '⌥⌘P');
  assert.equal(pc.label('mod+alt+p'), 'Ctrl+Alt+P');
});

test('named keys get a name rather than a lowercase word', () => {
  assert.equal(mac.label('enter'), '⏎');
  assert.equal(mac.label('escape'), 'Esc');
  assert.equal(mac.label('f2'), 'F2');
  // The delete key is ⌫ on a Mac and Del on a PC, and pointing at the wrong
  // one sends the reader hunting for a key their keyboard does not have.
  assert.equal(mac.label('delete'), '⌫');
  assert.equal(pc.label('delete'), 'Del');
});

test('Ctrl stands in for Command, so one table serves both platforms', () => {
  assert.ok(mac.matches(press({ metaKey: true, key: 'k', code: 'KeyK' }), 'mod+k'));
  assert.ok(mac.matches(press({ ctrlKey: true, key: 'k', code: 'KeyK' }), 'mod+k'));
});

test('Option is read from the physical key, not from the glyph it types', () => {
  // ⌥P types π on a Mac. Reading `key` here is how the shortcut silently
  // stopped working for exactly the people it was printed for.
  const event = press({ metaKey: true, altKey: true, key: 'π', code: 'KeyP' });
  assert.ok(mac.matches(event, 'mod+alt+p'));
});

test('modifiers match exactly, or ⌘K would fire on ⇧⌘K too', () => {
  const shifted = press({ metaKey: true, shiftKey: true, key: 'K', code: 'KeyK' });
  assert.equal(mac.matches(shifted, 'mod+k'), false);
  assert.ok(mac.matches(shifted, 'mod+shift+k'));

  // The one that matters while typing: a bare letter is never a shortcut.
  assert.equal(mac.matches(press({ key: 'b', code: 'KeyB' }), 'mod+b'), false);
});

test('punctuation is addressed by position, where a layout cannot move it', () => {
  assert.ok(mac.matches(press({ metaKey: true, key: ',', code: 'Comma' }), 'mod+,'));
});

test('every key in the shared table is one the matcher can recognise', () => {
  for (const combo of Object.values(mac.KEYS)) {
    assert.ok(mac.label(combo), `${combo} has no label`);
    const { mod, shift, alt, key } = {
      mod: combo.includes('mod'),
      shift: combo.includes('shift'),
      alt: combo.includes('alt'),
      key: combo.split('+').at(-1),
    };
    const event = press({
      metaKey: mod,
      shiftKey: shift,
      altKey: alt,
      key,
      code: `Key${key.toUpperCase()}`,
    });
    assert.ok(mac.matches(event, combo), `${combo} does not match its own keypress`);
  }
});

test('a keycap is an element, not markup, and is not read out twice', () => {
  const cap = mac.keycap('mod+k');
  assert.equal(cap.tagName, 'KBD');
  assert.equal(cap.className, 'kbd');
  assert.equal(cap.textContent, '⌘K');
  // The button beside it already says what it does.
  assert.equal(cap.getAttribute('aria-hidden'), 'true');
});
