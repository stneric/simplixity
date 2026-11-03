// Tests for the widget renderers:  node --test tests/frontend/
//
// These run on data the server has already validated, so the schema is not
// what is under test here. Two things are:
//
//   * **No markup, ever.** Every widget is built from values a model wrote, and
//     the whole layer exists so those values are laid out rather than
//     interpreted. A renderer that reached for innerHTML would undo that in one
//     line, and it would be invisible in review.
//   * **The fallbacks actually fire.** Both image proxies answer every failure
//     with HTTP 200 and a 1×1 transparent GIF, so `error` never fires and every
//     fallback hanging off it was dead code for as long as it existed. That is
//     what `onBrokenImage` is for, and it is the kind of bug only a test can
//     hold shut.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { find, installDom, tags, textOf, urls, withClass } from './dom.mjs';

installDom();
const { BlockView, renderers } = await import('../../web/js/render/blocks.js');

const SOURCES = [
  { index: 1, title: 'Wikipedia', url: 'https://en.wikipedia.org/x', domain: 'en.wikipedia.org' },
];

/** Every string that reached a node's text, for the assertion that matters
 *  most: nothing a model wrote may arrive as markup. */
const texts = (node, found = []) => {
  if (node.children?.length) for (const child of node.children) texts(child, found);
  else if (node.textContent) found.push(node.textContent);
  return found;
};

// ── series ───────────────────────────────────────────────────────────

const SERIES = {
  label: 'Bitcoin',
  unit: 'USD',
  change: '+18%',
  direction: 'up',
  as_of: '12 Aug 2026',
  points: [
    { at: '2023', value: '16500' },
    { at: '2024', value: '42000' },
    { at: '2025', value: '61000' },
  ],
  source: 1,
};

test('a series draws a curve and keeps the readings underneath it', () => {
  const out = renderers.series(SERIES, SOURCES);
  assert.equal(find(out, 'SVG').length, 1);
  // Three rows, because the curve says "rising" and only the table says from
  // what to what.
  assert.equal(find(out, 'TR').length, 3);
  assert.ok(texts(out).includes('16500'));
});

test('a series shows the source’s own text, not a reparsed number', () => {
  const out = renderers.series(
    { ...SERIES, points: [...SERIES.points, { at: '2026', value: 'about 70k' }] },
    SOURCES,
  );
  assert.ok(texts(out).includes('about 70k'));
  assert.equal(find(out, 'TR').length, 4); // the unplottable point keeps its row
});

test('a series with nothing plottable still renders its table', () => {
  const out = renderers.series(
    { ...SERIES, points: [{ at: '2024', value: 'n/a' }, { at: '2025', value: 'n/a' }] },
    SOURCES,
  );
  assert.equal(find(out, 'SVG').length, 0);
  assert.equal(find(out, 'TR').length, 2);
});

test('the direction colours the delta and nothing else', () => {
  const out = renderers.series(SERIES, SOURCES);
  const delta = withClass(out, 'series-delta')[0];
  assert.ok(delta.className.includes('up'));
  assert.equal(withClass(out, 'up').length, 1);
});

// ── comparison ───────────────────────────────────────────────────────

const COMPARISON = {
  title: 'DuckDB vs ClickHouse',
  left: { name: 'DuckDB' },
  right: { name: 'ClickHouse' },
  rows: [
    { label: 'Licence', left: 'MIT', right: 'Apache 2.0' },
    { label: 'Model', left: 'Embedded', right: 'Server' },
  ],
  source: 1,
};

test('a comparison lays out three columns per row plus the header', () => {
  const out = renderers.comparison(COMPARISON, SOURCES);
  const grid = withClass(out, 'comparison-grid')[0];
  assert.equal(grid.children.length, 9); // corner + 2 names + 2 rows × 3
  assert.deepEqual(
    withClass(out, 'comparison-name').map((n) => n.textContent),
    ['DuckDB', 'ClickHouse'],
  );
});

test('a comparison cell empty on one side renders as absent, not as a dash', () => {
  const out = renderers.comparison(
    { ...COMPARISON, rows: [{ label: 'Licence', left: 'MIT', right: '' }] },
    SOURCES,
  );
  assert.deepEqual(
    withClass(out, 'comparison-cell').map((n) => n.textContent),
    ['MIT', ''],
  );
});

// ── overview ─────────────────────────────────────────────────────────

const OVERVIEW = {
  kind: 'overview',
  sections: [
    {
      kind: 'timeline',
      title: 'Zeitleiste',
      items: [
        { label: '1989', text: 'Mauerfall', note: '', source: 1 },
        { label: '1990', text: 'Wiedervereinigung', note: '', source: 1 },
        { label: '1991', text: 'Hauptstadtbeschluss', note: '', source: 1 },
      ],
    },
    {
      kind: 'proscons',
      title: 'Abwägung',
      items: [
        { label: 'pro', text: 'billig', note: '', source: 1 },
        { label: 'con', text: 'laut', note: '', source: 1 },
      ],
    },
    {
      kind: 'grid',
      title: 'Modelle',
      columns: ['Preis', 'Reichweite'],
      items: [
        { label: 'A', text: '', note: '', cells: ['30k', ''], source: 1 },
        { label: 'B', text: '', note: '', cells: ['40k', '500 km'], source: 1 },
      ],
    },
  ],
};

test('an overview draws one section per block, in the order it was composed', () => {
  const out = renderers.overview(OVERVIEW, SOURCES);
  assert.deepEqual(
    withClass(out, 'overview-title').map((n) => n.textContent),
    ['Zeitleiste', 'Abwägung', 'Modelle'],
  );
  assert.ok(tags(out).includes('OL'));
  assert.ok(tags(out).includes('TABLE'));
});

test('a section never wears the class of the block inside it', () => {
  // `overview-callout` is the grey box; a section carrying it too drew its own
  // title inside the box. The kind lives in an attribute instead.
  const out = renderers.overview(
    { sections: [{ kind: 'callout', title: 'Frist', items: [{ text: 'x', source: 1 }] }] },
    SOURCES,
  );
  const [section] = withClass(out, 'overview-section');
  assert.equal(section.className, 'overview-section');
  assert.equal(section.getAttribute('data-kind'), 'callout');
  assert.equal(withClass(out, 'overview-callout').length, 1);
});

test('every overview item carries its own citation', () => {
  const out = renderers.overview(OVERVIEW, SOURCES);
  // 3 events + 2 arguments + 2 grid rows.
  assert.equal(withClass(out, 'overview-cite').length, 7);
});

test('a block kind the renderer does not know is skipped, not drawn raw', () => {
  const out = renderers.overview(
    { sections: [{ kind: 'mindmap', title: 'X', items: [{ text: '<b>x</b>', source: 1 }] }] },
    SOURCES,
  );
  assert.equal(withClass(out, 'overview-section').length, 0);
});

test('nothing a model wrote in an overview arrives as markup', () => {
  const hostile = {
    sections: [
      {
        kind: 'callout',
        title: '<img src=x onerror=alert(1)>',
        items: [{ label: '<b>Frist</b>', text: '<script>x</script>', note: '', source: 1 }],
      },
    ],
  };
  const out = renderers.overview(hostile, SOURCES);
  const all = texts(out);
  assert.ok(all.includes('<script>x</script>'));
  assert.ok(all.includes('<b>Frist</b>'));
  assert.ok(all.includes('<img src=x onerror=alert(1)>'));
});

// ── steps ────────────────────────────────────────────────────────────

test('the step numbers are rendered, so a model cannot skip one', () => {
  const out = renderers.steps(
    {
      title: 'Install the CLI',
      steps: [
        { text: 'Download the archive', detail: '' },
        { text: 'Run the installer', detail: './install.sh' },
      ],
      source: 1,
    },
    SOURCES,
  );
  assert.ok(tags(out).includes('OL'));
  // The pills carry no text of their own: a CSS counter fills them, so the
  // numbering cannot disagree with the list.
  assert.deepEqual(
    withClass(out, 'step-num').map((n) => n.textContent),
    ['', ''],
  );
  assert.equal(withClass(out, 'step-detail').length, 1);
});

// ── profile ──────────────────────────────────────────────────────────

const PROFILE = {
  name: 'Ada Lovelace',
  subtitle: 'Mathematician, 1815-1852',
  facts: [
    { label: 'Born', value: '1815' },
    { label: 'Known for', value: 'the first algorithm' },
  ],
  image: null,
  source: 1,
};

test('a profile without a portrait is still a profile', () => {
  const out = renderers.profile(PROFILE, SOURCES);
  assert.equal(withClass(out, 'profile-portrait').length, 0);
  assert.equal(textOf(out, 'profile-name'), 'Ada Lovelace');
  assert.equal(find(out, 'DD').length, 2);
});

test('a portrait is always proxied through this origin', () => {
  const out = renderers.profile(
    { ...PROFILE, image: { url: 'https://commons.test/ada.jpg', source: 1 } },
    SOURCES,
  );
  const src = urls(out).find((u) => u.startsWith('/api/image'));
  assert.equal(src, `/api/image?url=${encodeURIComponent('https://commons.test/ada.jpg')}`);
  // No request goes to the site the picture came from, so a reader who never
  // scrolled to it does not announce themselves to it.
  assert.ok(!urls(out).some((u) => u.startsWith('https://commons.test')));
});

test('a portrait that came back as the placeholder pixel removes its well', () => {
  // The proxy answers every failure with 200 and a 1×1 GIF so a laid-out widget
  // cannot reflow. That means `error` never fires, so the empty well has to be
  // detected some other way.
  const out = renderers.profile(
    { ...PROFILE, image: { url: 'https://commons.test/gone.jpg', source: 1 } },
    SOURCES,
  );
  const portrait = withClass(out, 'profile-portrait')[0];
  const img = find(portrait, 'IMG')[0];
  img.naturalWidth = 1;
  img.naturalHeight = 1;
  img.dispatch('load');
  assert.equal(portrait.removed, true);
});

test('a portrait that actually arrived is kept', () => {
  const out = renderers.profile(
    { ...PROFILE, image: { url: 'https://commons.test/ada.jpg', source: 1 } },
    SOURCES,
  );
  const img = find(out, 'IMG')[0];
  img.naturalWidth = 640;
  img.naturalHeight = 480;
  img.dispatch('load');
  assert.notEqual(withClass(out, 'profile-portrait')[0].removed, true);
});

// ── research ─────────────────────────────────────────────────────────

const STEPS_RUN = {
  title: 'Researching “Wer ist Donald Trump?”',
  sub_steps: [
    { text: 'Searching Web', status: 'done' },
    { text: '1 search · 6 relevant results', detail: 'Google, Bing', status: 'done' },
    { text: 'Found 6 pictures', status: 'active' },
  ],
};

test('a running search shows its steps', () => {
  const out = renderers.research({ ...STEPS_RUN, status: 'active' });
  assert.equal(find(out, 'DETAILS').length, 0);
  assert.equal(withClass(out, 'sub-step').length, 3);
  assert.ok(texts(out).includes('Searching Web'));
  // The engines sit on their own quieter line, not in the sentence.
  assert.equal(textOf(out, 'sub-step-detail'), 'Google, Bing');
});

test('a finished search folds behind one line, closed', () => {
  const out = renderers.research({ ...STEPS_RUN, status: 'done' });
  const details = find(out, 'DETAILS')[0];
  assert.ok(details);
  assert.ok(!details.open); // the answer is what the eye should land on
  // Still there to open: collapsing is not the same as discarding.
  assert.equal(withClass(out, 'sub-step').length, 3);
  assert.equal(textOf(out, 'research-count'), '3 steps');
  assert.ok(texts(out).includes('Researching “Wer ist Donald Trump?”'));
});

test('a finished plan folds behind its own line too', () => {
  const data = { sub_questions: ['one', 'two'], status: 'done' };
  const out = renderers.plan(data);
  const details = find(out, 'DETAILS')[0];
  assert.ok(details);
  assert.ok(!details.open);
  assert.equal(find(out, 'LI').length, 2);
  // While it is still planning the list is the news, so it stays open.
  assert.equal(find(renderers.plan({ ...data, status: 'active' }), 'DETAILS').length, 0);
});

test('a failed search folds too, and says so in its class', () => {
  const out = renderers.research({ ...STEPS_RUN, status: 'failed' });
  assert.equal(find(out, 'DETAILS').length, 1);
  assert.ok(out.classList.contains('failed'));
});

// ── sub-questions ────────────────────────────────────────────────────

const SUB = { question: 'What are the benchmarks?', findings: 'Some findings.', sources: [] };

test('a sub-question stays open while it is being researched', () => {
  const out = renderers.sub_question({ ...SUB, status: 'active' }, []);
  assert.ok(find(out, 'DETAILS')[0].open);
});

test('a sub-question folds as soon as its own research is finished', () => {
  // Deep research runs five of these. Left open, the report they are written
  // for starts several screens below the last one.
  for (const status of ['done', 'failed']) {
    const out = renderers.sub_question({ ...SUB, status }, []);
    assert.ok(!find(out, 'DETAILS')[0].open, status);
    // Folded, not discarded: the findings are still there to open.
    assert.ok(texts(out).includes('Some findings.'), status);
  }
});

test('a folded sub-question is the icon and the numbers it came back with', () => {
  const out = renderers.sub_question({ ...SUB, status: 'done', sources: [1] }, SOURCES);
  const badges = withClass(out, 'sub-question-badges')[0];
  assert.ok(badges);
  assert.deepEqual(
    badges.children.map((c) => c.textContent),
    ['1'],
  );
  // The question survives the fold. CSS hides it; the text is still there for
  // whoever opens it.
  assert.equal(textOf(out, 'sub-question-text'), SUB.question);
});

test('a sub-question that found nothing keeps its question on the line', () => {
  // With no numbers to stand in for it, the question is all the folded line
  // has. The CSS that hides it is keyed on the badges being there.
  const out = renderers.sub_question({ ...SUB, status: 'failed' }, []);
  assert.equal(withClass(out, 'sub-question-badges').length, 0);
  assert.equal(textOf(out, 'sub-question-text'), SUB.question);
});

// ── the view ─────────────────────────────────────────────────────────
// The renderers were right about folding for a whole release while nothing
// folded, because the view overwrote them: it copies the open flag off the node
// it is replacing, and that is the code path a renderer test cannot see.

const subQuestion = (status) => [
  { id: 's1', type: 'sub_question', data: { ...SUB, status, sources: [1] } },
];

const detailsOf = (container) => container.children[0].querySelector(':scope > details');

test('the view folds a sub-question when its status turns', () => {
  const container = document.createElement('div');
  const view = new BlockView(container);

  view.render(subQuestion('active'));
  assert.equal(detailsOf(container).open, true);

  view.render(subQuestion('done'));
  assert.equal(detailsOf(container).open, false);
});

test('the view leaves a fold the reader changed alone', () => {
  const container = document.createElement('div');
  const view = new BlockView(container);

  view.render(subQuestion('done'));
  detailsOf(container).open = true; // the reader opened it

  // A patch that does not change the state must not slam it shut again.
  view.render([
    { id: 's1', type: 'sub_question', data: { ...SUB, status: 'done', sources: [1], findings: 'More.' } },
  ]);
  assert.equal(detailsOf(container).open, true);
});

test('settling folds everything that is still open', () => {
  const container = document.createElement('div');
  const view = new BlockView(container);

  view.render(subQuestion('active'));
  view.settle();

  assert.equal(detailsOf(container).open, false);
  assert.ok(container.children[0].classList.contains('settled'));
});

test('the fold survives a patch, and the reader still gets the last word', () => {
  const active = renderers.sub_question({ ...SUB, status: 'active' }, []);
  const finished = renderers.sub_question({ ...SUB, status: 'done' }, []);
  const same = renderers.sub_question({ ...SUB, status: 'done', findings: 'More.' }, []);

  // A state change wins over whatever the previous node had…
  assert.notEqual(
    find(active, 'DETAILS')[0].dataset.status,
    find(finished, 'DETAILS')[0].dataset.status,
  );
  // …while a patch that leaves the state alone must not, or a reader who opened
  // a finished sub-question would have it snap shut under them.
  assert.equal(
    find(finished, 'DETAILS')[0].dataset.status,
    find(same, 'DETAILS')[0].dataset.status,
  );
});

// ── provenance ───────────────────────────────────────────────────────

const RUN = {
  depth_label: 'Simple',
  requested_depth_label: 'Auto',
  search: { queries: 1, engines: [{ name: 'duckduckgo', results: 6 }] },
  sources: { total: 11, read: 0, tiers: {} },
  stats: {
    elapsed_s: 14.2,
    llm_calls: 3,
    prompt_tokens: 9000,
    completion_tokens: 3400,
    reasoning_tokens: 2100,
    total_tokens: 12400,
    cost_usd: 0.0038,
  },
};

test('the footer says what the run spent, without being opened', () => {
  const line = textOf(renderers.provenance(RUN), 'prov-line');
  assert.match(line, /12\.4k tokens/);
  assert.match(line, /\$0\.0038/);
});

test('fractions of a cent keep their digits instead of rounding to nothing', () => {
  const cheap = { ...RUN, stats: { ...RUN.stats, cost_usd: 0.0000866 } };
  assert.match(textOf(renderers.provenance(cheap), 'prov-line'), /\$0\.0001/);
});

test('a model nobody quoted a price for claims no price', () => {
  const local = { ...RUN, stats: { ...RUN.stats, cost_usd: null } };
  const line = textOf(renderers.provenance(local), 'prov-line');
  assert.match(line, /12\.4k tokens/);
  assert.ok(!line.includes('$'), line);
  // Free is a price, and it is not the same as unknown.
  const free = { ...RUN, stats: { ...RUN.stats, cost_usd: 0 } };
  assert.match(textOf(renderers.provenance(free), 'prov-line'), /free/);
});

test('reasoning tokens are broken out, because they are most of the bill', () => {
  const out = renderers.provenance(RUN);
  const row = withClass(out, 'prov-row').find((r) => r.children[0].textContent === 'Model tokens');
  assert.match(row.children[1].textContent, /12400 total: 9000 in, 3400 out, 2100 of them reasoning/);
});

test('the ranking meter is reported next to the models, never added to them', () => {
  // 456k tokens against $0.081 was the footer of a real run, and the arithmetic
  // does not work: almost all of it was free embedding tokens from ranking the
  // source list once per sub-question. Summed, the price reads as a mistake.
  const ranked = {
    ...RUN,
    stats: { ...RUN.stats, embedding: { total_tokens: 448000, cost_usd: 0 } },
  };
  const out = renderers.provenance(ranked);
  const rows = withClass(out, 'prov-row');
  const model = rows.find((r) => r.children[0].textContent === 'Model tokens');
  const embed = rows.find((r) => r.children[0].textContent === 'Embedding tokens');
  assert.match(model.children[1].textContent, /^12400 total/);
  assert.match(embed.children[1].textContent, /448000 embedded for ranking, free/);
  // And the one-liner still counts only what a language model was asked to read.
  assert.match(textOf(out, 'prov-line'), /12\.4k tokens/);
});

test('a run that never embedded anything grows no empty row for it', () => {
  const rows = withClass(renderers.provenance(RUN), 'prov-row');
  assert.equal(rows.filter((r) => r.children[0].textContent === 'Embedding tokens').length, 0);
});

// ── the rule that covers all of them ─────────────────────────────────

test('no widget renderer ever emits markup from a value', () => {
  const injected = '<img src=x onerror=alert(1)>';
  const cases = [
    ['series', { ...SERIES, label: injected, points: SERIES.points }],
    ['comparison', { ...COMPARISON, left: { name: injected }, right: { name: 'y' } }],
    ['steps', { title: injected, steps: [{ text: injected, detail: injected }], source: 1 }],
    ['profile', { ...PROFILE, name: injected, subtitle: injected }],
  ];
  for (const [kind, data] of cases) {
    const out = renderers[kind](data, SOURCES);
    // The string survives as text. It is not escaped away, it is simply never
    // parsed, and no element was created from it.
    assert.ok(texts(out).includes(injected), kind);
    assert.ok(!find(out, 'IMG').length, kind);
  }
});

// ── citation check ───────────────────────────────────────────────────
// The block that describes the answer rather than the evidence. What matters is
// that a finding cannot hide: the summary line has to state it, and the body has
// to be open before anyone clicks.

const CHECK = {
  total: 3,
  checked: 3,
  supported: 2,
  partial: 1,
  unsupported: 0,
  unchecked: 0,
  claims: [
    { text: 'The limiter needs Valkey.', sources: [1], verdict: 'supported', note: '' },
    { text: 'Blocked within a day.', sources: [1, 2], verdict: 'supported', note: '' },
    {
      text: 'The ban is permanent.',
      sources: [2],
      verdict: 'partial',
      note: 'the source gives no duration',
    },
  ],
};

test('a clean check states it in one line and stays folded', () => {
  const clean = {
    ...CHECK,
    total: 2,
    checked: 2,
    supported: 2,
    partial: 0,
    claims: CHECK.claims.slice(0, 2),
  };
  const out = renderers.verification(clean);
  assert.match(textOf(out, 'verify-line'), /All 2 cited statements supported/);
  assert.equal(find(out, 'details')[0].open, false);
  assert.equal(withClass(out, 'flagged').length + (out.className.includes('flagged') ? 1 : 0), 0);
});

test('a finding opens the block and names the count', () => {
  const out = renderers.verification(CHECK);
  assert.match(textOf(out, 'verify-line'), /2 of 3 cited statements supported/);
  assert.match(textOf(out, 'verify-line'), /1 only partly/);
  assert.ok(out.className.includes('flagged'));
  assert.equal(find(out, 'details')[0].open, true);
});

test('each claim carries its verdict, its sources and any note', () => {
  const out = renderers.verification(CHECK);
  const rows = withClass(out, 'verify-claim');
  assert.equal(rows.length, 3);
  assert.equal(textOf(rows[2], 'verify-chip'), 'Partly');
  assert.equal(textOf(rows[1], 'verify-cites'), '[1] [2]');
  assert.equal(textOf(rows[2], 'verify-note'), 'the source gives no duration');
});

test('the block says what the check does and does not claim', () => {
  const out = renderers.verification(CHECK);
  assert.match(textOf(out, 'verify-scope'), /not whether it is true/);
});

test('a claim written by a model never arrives as markup', () => {
  const out = renderers.verification({
    ...CHECK,
    claims: [{ text: '<img src=x onerror=alert(1)>', sources: [1], verdict: 'unsupported', note: '<b>x</b>' }],
  });
  assert.ok(!tags(out).includes('IMG') && !tags(out).includes('B'));
  assert.ok(texts(out).some((text) => text.includes('<img src=x')));
});

test('while the check runs the eye reads, and it says so', () => {
  const out = renderers.verification({ status: 'active', checked: 4, claims: [] });
  assert.match(textOf(out, 'verify-line'), /Checking the answer against its sources/);
  // The pupil is a node of its own inside the icon; that is what CSS animates.
  assert.match(withClass(out, 'icon-slot')[0].innerHTML, /class="eye-pupil"/);
  assert.equal(find(out, 'details').length, 0);
});

test('a check that could not run says the answer is unchecked', () => {
  const out = renderers.verification({ status: 'failed', total: 4, claims: [] });
  assert.match(textOf(out, 'verify-line'), /could not run/);
  assert.ok(out.className.includes('flagged'));
});

test('a check there was no budget left for says that, rather than nothing at all', () => {
  // An absent block reads exactly like a check that ran and found nothing wrong.
  const out = renderers.verification({ status: 'skipped', total: 4, claims: [] });
  assert.match(textOf(out, 'verify-line'), /No budget left/);
  assert.ok(out.className.includes('flagged'));
});

test('the line counts what the answer cited, not only what the check got to', () => {
  // The bug this pins: an answer with 38 cited sentences and a 14-claim limit
  // was summarised as "All 14 cited statements supported by their sources".
  const out = renderers.verification({
    total: 38,
    checked: 14,
    supported: 14,
    partial: 0,
    unsupported: 0,
    unchecked: 24,
    claims: [{ text: 'Checked.', sources: [1], verdict: 'supported', note: '' }],
  });
  assert.match(textOf(out, 'verify-line'), /14 of 38 cited statements checked, all supported/);
});

test('an unchecked claim is listed as such and does not raise a warning', () => {
  const data = {
    total: 2,
    checked: 1,
    supported: 1,
    partial: 0,
    unsupported: 0,
    unchecked: 1,
    claims: [
      { text: 'The limiter needs Valkey.', sources: [1], verdict: 'supported', note: '' },
      { text: 'Blocked within a day.', sources: [2], verdict: 'unchecked', note: '' },
    ],
  };
  const out = renderers.verification(data);
  assert.equal(textOf(withClass(out, 'verify-claim')[1], 'verify-chip'), 'Not checked');
  // A gap in the check is not a finding against the answer: it belongs in the
  // line, not in the colour, or every long answer arrives pre-flagged.
  assert.ok(!out.className.includes('flagged'));
  assert.equal(find(out, 'details')[0].open, false);
});

test('a run stored before the counts were split still renders as it did', () => {
  const { total, unchecked, ...old } = CHECK;
  assert.match(textOf(renderers.verification(old), 'verify-line'), /2 of 3 cited statements/);
});

test('an answer that cites nothing is flagged, and names what it wrote instead', () => {
  // No block at all is what a clean check looks like, so the one answer that
  // ignored the citation contract must not end up with none.
  const out = renderers.verification({
    status: 'uncited',
    claims: [],
    placeholders: ['Quelle impliziert durch den Kontext'],
  });

  assert.match(textOf(out, 'verify-line'), /cites no sources/);
  assert.match(textOf(out, 'verify-line'), /Quelle impliziert durch den Kontext/);
  assert.ok(withClass(out, 'flagged').length > 0, 'it must read as a problem');
});

test('an uncited answer with no placeholder still says nothing was checked', () => {
  const out = renderers.verification({ status: 'uncited', claims: [], placeholders: [] });
  assert.match(textOf(out, 'verify-line'), /nothing in it could be checked/);
});

// ── the tray ─────────────────────────────────────────────────────────
//
// Everything about an answer that is not the answer folds into one row of
// icons under it. What the reader has to see stays in the transcript as text:
// a finding behind an icon is a finding nobody reads.

function turn() {
  const host = document.createElement('article');
  const container = document.createElement('div');
  host.append(container);
  return { host, container, view: new BlockView(container) };
}

const chipIds = (view) => view.trayIcons.children.map((chip) => chip.dataset.chip);
const CLEAN = { status: 'done', total: 1, checked: 1, supported: 1, claims: [{ verdict: 'supported', text: 'x' }] };
const FLAGGED = {
  status: 'done', total: 2, checked: 2, supported: 1, partial: 1,
  claims: [{ verdict: 'supported', text: 'x' }, { verdict: 'partial', text: 'y' }],
};
const finishedRun = (verification) => [
  { id: 'r', type: 'research', data: { title: 'Researching', status: 'done', sub_steps: [{ text: 'a', status: 'done' }] } },
  { id: 'src', type: 'sources', data: { sources: SOURCES } },
  { id: 't', type: 'text', data: { content: 'The answer [1].' } },
  { id: 'v', type: 'verification', data: verification },
  { id: 'p', type: 'provenance', data: RUN },
];

test('a finished answer keeps only its prose in the transcript', () => {
  const { container, view } = turn();
  view.render(finishedRun(CLEAN));
  view.settle();

  assert.equal(container.children.length, 1);
  assert.ok(container.children[0].classList.contains('text'));
  assert.deepEqual(chipIds(view), ['#copy', 'r', 'src', 'v', 'p']);
  // Each block's summary line is its icon's tooltip.
  assert.match(view.chips.get('src').title, /1 source/);
  assert.match(view.chips.get('p').title, /Auto → Simple/);
});

test('a citation check that found something stays in the transcript', () => {
  const { container, view } = turn();
  view.render(finishedRun(FLAGGED));
  view.settle();

  assert.ok(container.children.some((node) => node.classList.contains('verification')));
  assert.ok(!chipIds(view).includes('v'));
});

test('an icon opens its block under the row, and closes it again', () => {
  const { view } = turn();
  view.render(finishedRun(CLEAN));
  view.settle();
  const sources = view.nodes.get('src').node;
  assert.equal(sources.hidden, true);

  view.chips.get('src').dispatch('click');
  assert.equal(sources.hidden, false);
  assert.equal(sources.querySelector(':scope > details').open, true);
  assert.equal(view.nodes.get('p').node.hidden, true);

  view.chips.get('src').dispatch('click');
  assert.equal(sources.hidden, true);
});

test('copy is offered only once the answer is finished', () => {
  const { view } = turn();
  view.render(finishedRun(CLEAN));
  assert.ok(!chipIds(view).includes('#copy'));
  view.settle();
  assert.equal(chipIds(view)[0], '#copy');
});

test('a research box still working stays where it can be watched', () => {
  const { container, view } = turn();
  view.render([{ id: 'r', type: 'research', data: { title: 'Researching', status: 'active', sub_steps: [] } }]);
  assert.ok(container.children[0].classList.contains('research'));
  view.settle();
  assert.equal(container.children.length, 0);
  assert.deepEqual(chipIds(view), ['r']);
});

test('the tray sits after the blocks and a note goes above it', () => {
  const { host, container, view } = turn();
  view.render(finishedRun(CLEAN));
  view.setMeta({ text: '22:11 · gemini', title: 'Thursday · gemini' });
  const note = document.createElement('div');
  view.note(note);

  assert.deepEqual(host.children, [container, note, view.tray]);
  assert.equal(chipIds(view).at(-1), '#meta');
});
