// The name a profile is stored under. The server drops anything outside
// `a-z0-9_-`, so the browser folds a typed name into that shape first; a name
// that silently became "default" would open someone else's history.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { installDom } from './dom.mjs';

installDom();
const { slug } = await import('../../web/profile.js');

test('folds case, accents and spaces into a cookie-safe name', () => {
  assert.equal(slug('Eric'), 'eric');
  assert.equal(slug('Jürgen Müller'), 'jurgen-muller');
  assert.equal(slug('  anna_b  '), 'anna_b');
});

test('never produces something the server would reject', () => {
  for (const typed of ['a/b\\c', '💥 boom', 'x'.repeat(80), '--x--']) {
    assert.match(slug(typed), /^[a-z0-9_-]{0,32}$/);
  }
  assert.equal(slug('!!!'), '');
  assert.equal(slug(null), '');
});
