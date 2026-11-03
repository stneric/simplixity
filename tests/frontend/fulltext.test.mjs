// The full-text view's pure half, and the composer's literature-project
// switch: what is grouped where, what a file's line says, which papers a file
// may be assigned to, and when the composer swaps its chips for the switch.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { assignChoices, fileLine, fulltextOverview, matchLabel, warningText } from '../../web/js/literature/literature-core.js';
import { isLibrary, libraryBadge } from '../../web/js/chat/source-switch.js';

const papers = [
  { id: 1, decision: 'relevant', data: { title: 'A' } },
  { id: 2, decision: 'relevant', data: { title: 'B' } },
  { id: 3, decision: 'maybe', data: { title: 'C' } },
  { id: 4, decision: 'excluded', data: { title: 'D' } },
  { id: 5, decision: '', data: { title: 'E' } },
];

const files = [
  { id: 'f1', paper_id: 1, pages: 10, text_pages: 10, chunks: 20, embedded: 20, match: 'doi' },
  { id: 'f2', paper_id: null, pages: 4, text_pages: 4, chunks: 8, embedded: 0, match: '' },
  { id: 'f3', paper_id: 3, pages: 6, text_pages: 2, chunks: 5, embedded: 5, match: 'manual' },
];

test('the overview groups by what the person has to do', () => {
  const view = fulltextOverview(papers, files);
  assert.equal(view.relevant, 2);
  assert.deepEqual(view.ready.map((r) => r.paper.id), [1]);
  assert.deepEqual(view.missing.map((p) => p.id), [2]);
  assert.deepEqual(view.unassigned.map((f) => f.id), ['f2']);
  // Filed under a maybe: kept, shown, and said not to be searched.
  assert.deepEqual(view.idle.map((r) => r.paper.id), [3]);
  assert.equal(view.unindexed, 8);
});

test('a file line says pages, text, match and index state', () => {
  assert.equal(fileLine(files[0]), '10 pages · matched by DOI · indexed');
  assert.equal(fileLine(files[1]), '4 pages · keyword search only');
  assert.equal(fileLine(files[2]), '6 pages · 2 with text · assigned by you · indexed');
  assert.equal(fileLine({ pages: 1, text_pages: 0, chunks: 0, embedded: 0 }), '1 page · 0 with text · nothing to search');
  assert.equal(matchLabel('nonsense'), '');
  assert.match(warningText('scanned'), /scan/);
});

test('a file is offered its candidates first and never an excluded paper', () => {
  assert.deepEqual(assignChoices(papers, [3]).map((p) => p.id), [3, 1, 2]);
  assert.ok(!assignChoices(papers).some((p) => p.id === 4 || p.id === 5));
});

test('a project is a literature project when a reading list is filed under it', () => {
  assert.ok(isLibrary({ lists: 1 }));
  assert.ok(!isLibrary({ lists: 0 }));
  assert.ok(!isLibrary(null));
});

test('the badge says what the next answer is written from', () => {
  const project = { name: 'Thesis', lists: 1, relevant: 23, fulltexts: 12 };
  assert.equal(libraryBadge(project, false), '“Thesis”: answers only from your papers (23 relevant papers, 12 with full text)');
  assert.match(libraryBadge(project, true), /and the web$/);
  assert.equal(libraryBadge({ name: 'Errands', lists: 0 }, false), '');
});
