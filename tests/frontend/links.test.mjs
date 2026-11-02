// Tests for the shared link helpers:  node --test tests/frontend/
//
// One helper builds every outbound link, so it is the one place a scheme check
// can be forgotten. The server only registers http(s) sources today; these hold
// the client to the same rule for whatever reaches it tomorrow.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { installDom, withClass } from './dom.mjs';

installDom();
const { citeLink, outbound, safeUrl } = await import('../../web/links.js');
const { renderers } = await import('../../web/blocks.js');

test('only http and https survive the scheme check', () => {
  assert.equal(safeUrl('https://example.org/a'), 'https://example.org/a');
  assert.equal(safeUrl('javascript:alert(1)'), null);
  assert.equal(safeUrl('data:text/html,<b>x</b>'), null);
});

test('an unsafe link keeps its node and loses its target', () => {
  const node = outbound('javascript:alert(1)', 'x');
  assert.equal(node.tagName, 'A');
  assert.ok(!node.href);
  const safe = outbound('https://example.org/', 'x');
  assert.equal(safe.target, '_blank');
  assert.equal(safe.rel, 'noopener noreferrer');
});

test('a citation names its source for screen readers and links to it', () => {
  const sources = [{ index: 2, title: 'Paper', url: 'https://example.org/p' }];
  const node = citeLink(2, sources);
  assert.equal(node.textContent, '2');
  assert.equal(node.href, 'https://example.org/p');
  assert.equal(citeLink(9, sources), null);
});

test('a widget source line never links a script URL', () => {
  const sources = [{ index: 1, title: 'x', url: 'javascript:alert(1)', domain: 'evil' }];
  const out = renderers.steps({ title: 't', steps: [{ text: 'a' }, { text: 'b' }], source: 1 }, sources);
  const [cite] = withClass(out, 'widget-cite');
  assert.ok(cite);
  assert.ok(!cite.href);
});
