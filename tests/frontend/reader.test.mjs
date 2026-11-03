// The reader's pure half: what a citation carries to the reader, and where on
// a rendered page a passage stands when the page's text comes split into items
// the way pdf.js splits it.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { locate, parseHash, sourceHash } from '../../web/js/reader/reader-core.js';

test('a citation carries its passage, evidence and context in the fragment, and back', () => {
  const source = { evidence: ['all of the keys & values'], passages: ['A #hash? passage'] };
  const hash = sourceHash(source, ['we scale the dot products']);
  assert.ok(hash.startsWith('#'));
  assert.deepEqual(parseHash(hash), {
    cited: ['we scale the dot products'],
    evidence: ['all of the keys & values'],
    passages: ['A #hash? passage'],
  });
  assert.equal(sourceHash({}), '');
});

// How pdf.js hands over a page: pieces of lines, a hyphenated line end, a
// ligature, and no spaces between items.
const items = [
  '3.2.3 Applications of Attention',
  'The encoder contains self-attention layers. In a self-attention layer all of the',
  'keys, values and queries come from the same place, in this case, the out-',
  'put of the previous layer in the encoder.',
  'Similarly, self-attention layers in the decoder allow each position to attend.',
];

test('a passage across lines and a hyphenated break is found whole', () => {
  const found = locate(items, 'all of the keys, values and queries come from the same place, in this case, the output of the previous layer');
  assert.deepEqual(found, [1, 2, 3]);
});

test('a passage our extraction read differently still lands by its windows', () => {
  // pypdf read "queries" as "querles"; the rest of the sentence still marks.
  const found = locate(items, 'In a self-attention layer all of the keys, values and querles come from the same place, in this case, the output of the previous layer in the encoder.');
  assert.ok(found.includes(1) && found.includes(3));
});

test('something not on the page marks nothing', () => {
  assert.deepEqual(locate(items, 'The Eiffel Tower is three hundred metres tall and stands in Paris.'), []);
  assert.deepEqual(locate(items, 'the encoder'), [], 'too short to prove anything');
});
