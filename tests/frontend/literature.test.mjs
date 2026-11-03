// The literature page's pure half: what is counted, what a filter shows, which
// card takes focus after a decision, which summaries are asked for first, and
// how a round's report is said.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  authorLine,
  briefFromForm,
  describedLabel,
  focusAfter,
  keyAction,
  metaLine,
  originLabel,
  pendingSummaries,
  roundSummary,
  searchable,
  sourceLine,
  tally,
  visible,
} from '../../web/js/literature/literature-core.js';

const papers = [
  { id: 1, decision: '', summary: null },
  { id: 2, decision: 'relevant', summary: { summary: 'x' } },
  { id: 3, decision: '', summary: { summary: 'y' } },
  { id: 4, decision: 'excluded', summary: null },
  { id: 5, decision: '', summary: null },
];

test('tally counts undecided papers as open', () => {
  assert.deepEqual(tally(papers), { open: 3, relevant: 1, maybe: 0, excluded: 1, all: 5 });
});

test('filters keep the server order', () => {
  assert.deepEqual(visible(papers, 'open').map((p) => p.id), [1, 3, 5]);
  assert.deepEqual(visible(papers, 'excluded').map((p) => p.id), [4]);
  assert.equal(visible(papers, 'all'), papers);
});

test('focus moves to the next card, or back at the end of the list', () => {
  const open = visible(papers, 'open');
  assert.equal(focusAfter(open, 3), 5);
  assert.equal(focusAfter(open, 5), 3);
  assert.equal(focusAfter([{ id: 9 }], 9), null);
});

test('summaries are asked for on-screen papers first, then the rest of the undecided', () => {
  assert.deepEqual(pendingSummaries(papers, 'excluded'), [4, 1, 5]);
  assert.deepEqual(pendingSummaries(papers, 'open'), [1, 5]);
  // A failed one is not asked for again until the person retries.
  assert.deepEqual(pendingSummaries(papers, 'open', new Set([1])), [5]);
});

test('authors shorten to surnames and et al.', () => {
  assert.equal(authorLine(['Ada Lovelace', 'Turing, Alan', 'Grace Hopper', 'X Y']), 'Lovelace, Turing, Hopper et al.');
  assert.equal(authorLine([]), '');
});

test('the meta line names year, venue, kind and citations', () => {
  assert.equal(
    metaLine({ year: 2021, venue: 'IEEE TSG', kind: 'journal', citations: 1312 }),
    '2021 · IEEE TSG · Journal · 1,312 citations',
  );
  assert.equal(metaLine({ citations: 0, kind: 'other' }), '');
  assert.equal(sourceLine(['ieee', 'openalex']), 'IEEE Xplore · OpenAlex');
});

test('the origin explains a paper the search did not find', () => {
  assert.equal(originLabel({ origin: 'search', data: {} }), '');
  assert.equal(originLabel({ origin: 'references', data: { links: 3 } }), 'Cited by 3 of your relevant papers');
  assert.equal(originLabel({ origin: 'citations', data: { links: 1 } }), 'Cites 1 of your relevant papers');
  assert.equal(originLabel({ origin: 'recommended', data: {} }), 'Recommended from your picks');
});

test('a paper the person brought says so, and how sure its title is', () => {
  assert.equal(originLabel({ origin: 'upload', data: {} }), 'Added by you');
  assert.equal(describedLabel('registry'), 'described from OpenAlex');
  assert.match(describedLabel('file'), /file name/);
  assert.equal(describedLabel(''), '');
});

test('screening keys are plain keys only', () => {
  assert.equal(keyAction({ key: '1' }), 'relevant');
  assert.equal(keyAction({ key: 'j' }), 'next');
  assert.equal(keyAction({ key: 'u' }), 'undo');
  assert.equal(keyAction({ key: '1', metaKey: true }), null);
  assert.equal(keyAction({ key: 'x' }), null);
});

test('a round with problems says each one', () => {
  const { headline, problems } = roundSummary({
    round: 2,
    added: 1,
    found: 40,
    sources: [{ label: 'Semantic Scholar', errors: ['rate-limited'] }],
    strategies: [{ label: 'Citing your relevant papers', errors: [] }],
    notes: ['Ranking fell back to keywords.'],
  });
  assert.equal(headline, 'Round 2: 1 new paper to screen, from 40 found.');
  assert.deepEqual(problems, ['Semantic Scholar: rate-limited', 'Ranking fell back to keywords.']);
  assert.match(roundSummary({ round: 3, added: 0, found: 12 }).headline, /nothing new/);
});

test('the brief form round-trips into the API shape', () => {
  const brief = briefFromForm({
    question: '  FL for grids ',
    include: 'peer-reviewed\n\n  2018 or later ',
    exclude: '',
    queries: 'federated learning smart grid\n',
    year_from: '2018',
    year_to: 'abc',
  });
  assert.deepEqual(brief.include, ['peer-reviewed', '2018 or later']);
  assert.deepEqual(brief.exclude, []);
  assert.equal(brief.year_from, 2018);
  assert.equal(brief.year_to, 0);
  assert.equal(brief.question, 'FL for grids');
  assert.ok(searchable(brief));
  assert.ok(!searchable({ question: ' ', queries: [] }));
});
