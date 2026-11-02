// Tests for the vendored markdown renderer:  node --test tests/frontend/
//
// It runs on untrusted model output on every token of every answer, so the two
// properties that matter are that it cannot emit markup or a dangerous URL, and
// that it cannot hang the tab.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { find, installDom, tags, urls } from './dom.mjs';

installDom();
const { citedText, paperNumbers, referenceLabel, renderMarkdown } = await import('../../web/markdown.js');

const SOURCES = [
  { index: 1, title: 'Redis Docs', url: 'https://redis.io/x' },
  { index: 2, title: 'Wikipedia', url: 'https://en.wikipedia.org/y' },
];

function render(markdown, sources = SOURCES) {
  const root = document.createElement('div');
  renderMarkdown(root, markdown, { sources });
  return root;
}

// ── structure ────────────────────────────────────────────────────────

test('headings, emphasis and inline code', () => {
  const out = render('# Title\n\nA **bold** and *italic* and `code` line.');
  assert.deepEqual(tags(out), ['DIV', 'H1', 'P', 'STRONG', 'EM', 'CODE']);
});

test('nested lists keep their nesting', () => {
  const out = render('- one\n- two\n  - nested\n- three');
  assert.equal(tags(out).filter((t) => t === 'UL').length, 2);
  assert.equal(tags(out).filter((t) => t === 'LI').length, 4);
});

test('ordered lists render as ol', () => {
  assert.ok(tags(render('1. first\n2. second')).includes('OL'));
});

// The failure this guards: a blank line between items started a new list, so
// every item of a loose ordered list was item 1 of its own `<ol>` and the
// answer counted "1. 1. 1.".
test('a blank line between items keeps one list, numbered through', () => {
  const out = render('1. first\n\n2. second\n\n3. third');
  assert.equal(tags(out).filter((t) => t === 'OL').length, 1);
  assert.equal(tags(out).filter((t) => t === 'LI').length, 3);
});

test('a loose bullet list is one list', () => {
  const out = render('- one\n\n- two');
  assert.equal(tags(out).filter((t) => t === 'UL').length, 1);
  assert.equal(tags(out).filter((t) => t === 'LI').length, 2);
});

test('a blank line before a paragraph still ends the list', () => {
  const out = render('- one\n- two\n\nA following paragraph.');
  assert.equal(tags(out).filter((t) => t === 'LI').length, 2);
  assert.equal(find(out, 'p')[0].textContent, 'A following paragraph.');
});

test('a loose list keeps its indented continuation lines', () => {
  const out = render('1. first\n\n   more about first\n\n2. second');
  assert.equal(tags(out).filter((t) => t === 'OL').length, 1);
  assert.equal(tags(out).filter((t) => t === 'LI').length, 2);
  assert.match(out.textContent, /more about first/);
});

test('tables get a header row and a scroll wrapper', () => {
  const out = render('| a | b |\n|---|---|\n| 1 | 2 |');
  const found = tags(out);
  assert.ok(found.includes('TABLE') && found.includes('THEAD') && found.includes('TBODY'));
  assert.equal(found.filter((t) => t === 'TH').length, 2);
  // The wrapper is what stops a wide table from stretching the page.
  assert.equal(out.children[0].className, 'table-wrap');
});

test('fenced code keeps its text verbatim', () => {
  const out = render('```python\ndef f(x):\n    return x ** 2\n```');
  assert.match(out.textContent, /return x \*\* 2/);
  assert.ok(tags(out).includes('PRE'));
});

test('blockquotes and rules', () => {
  const found = tags(render('> quoted\n\n---\n\nafter'));
  assert.ok(found.includes('BLOCKQUOTE') && found.includes('HR'));
});

// ── citations ────────────────────────────────────────────────────────

test('citation markers become links to their source', () => {
  const out = render('Fast [1] and popular [2].');
  assert.deepEqual(urls(out), ['https://redis.io/x', 'https://en.wikipedia.org/y']);
});

test('a citation with no matching source stays literal text', () => {
  // A dead link would imply evidence that does not exist.
  const out = render('Nothing backs this [9].');
  assert.equal(urls(out).length, 0);
  assert.match(out.textContent, /\[9\]/);
});

// ── safety ───────────────────────────────────────────────────────────

test('html in model output is text, never markup', () => {
  const out = render('<script>alert(1)</script> and <img src=x onerror=alert(2)>');
  assert.equal(tags(out).filter((t) => t === 'SCRIPT' || t === 'IMG').length, 0);
  assert.match(out.textContent, /<script>/);
});

test('javascript: and data: urls never reach an href or src', () => {
  const out = render('[click](javascript:alert(1))\n\n![x](data:text/html,<script>)');
  assert.deepEqual(urls(out), []);
});

test('only http(s) links survive', () => {
  const out = render('[a](https://ok.test/x) [b](ftp://no.test/y) [c](mailto:x@y.z)');
  assert.deepEqual(urls(out), ['https://ok.test/x']);
});

// ── robustness ───────────────────────────────────────────────────────

test('a bare url does not recurse forever', () => {
  // An autolink rule that matches its own output hangs the tab.
  const out = render('See https://bare.example.com/b for details');
  assert.deepEqual(urls(out), ['https://bare.example.com/b']);
});

test('a half-streamed code fence still renders', () => {
  // Every intermediate state of a streaming answer is rendered, including
  // this one.
  assert.doesNotThrow(() => render('## Partial\n\n```python\ndef f():'));
});

test('empty and whitespace input produce nothing, not an error', () => {
  assert.equal(render('').children.length, 0);
  assert.equal(render('   \n\n  ').children.length, 0);
});

test('pathological input finishes quickly', () => {
  const started = Date.now();
  render('*'.repeat(400) + '\n\n' + '['.repeat(400) + '\n\n' + '`'.repeat(400));
  assert.ok(Date.now() - started < 2000);
});

// ── numeric layout ───────────────────────────────────────────────────
// A price, a forecast and a league table are the answers where layout *is* the
// content: a column of numbers ragged-left is unreadable, and models write the
// `---` divider about half the time instead of `---:`.

test('declared column alignment is honoured', () => {
  const root = document.createElement('div');
  renderMarkdown(root, ['| A | B | C |', '| :--- | ---: | :---: |', '| x | 1 | y |'].join('\n'));
  const [a, b, c] = find(root, 'th');
  assert.equal(a.dataset.align, 'left');
  assert.equal(b.dataset.align, 'right');
  assert.equal(c.dataset.align, 'center');
});

test('an all-numeric column is right-aligned even when the divider says nothing', () => {
  const root = document.createElement('div');
  renderMarkdown(
    root,
    ['| Day | High °C |', '| --- | --- |', '| Wed | 24 |', '| Thu | 21.5 |'].join('\n'),
  );
  const [day, high] = find(root, 'th');
  assert.equal(day.dataset.align, undefined);
  assert.equal(high.dataset.align, 'right');
});

test('a mostly-text column is never inferred as numeric', () => {
  const root = document.createElement('div');
  renderMarkdown(
    root,
    ['| Team | Note |', '| --- | --- |', '| A | won 2 |', '| B | 3 |', '| C | drew |'].join('\n'),
  );
  assert.equal(find(root, 'th')[1].dataset.align, undefined);
});

test('currency, percentages and signed deltas count as numbers', () => {
  const root = document.createElement('div');
  renderMarkdown(
    root,
    ['| K | V |', '| --- | --- |', '| a | $231.40 |', '| b | +1.8% |', '| c | −0.4% |'].join('\n'),
  );
  assert.equal(find(root, 'th')[1].dataset.align, 'right');
});

test('a citation in a cell does not stop it counting as a number', () => {
  const root = document.createElement('div');
  const sources = [{ index: 1, url: 'https://a.test', title: 'A' }];
  renderMarkdown(
    root,
    ['| K | V |', '| --- | --- |', '| a | 24 [1] |', '| b | 21 [1] |'].join('\n'),
    { sources },
  );
  assert.equal(find(root, 'th')[1].dataset.align, 'right');
});

test('a headerless two-column table becomes a key/value block', () => {
  const root = document.createElement('div');
  renderMarkdown(root, ['|  |  |', '| --- | --- |', '| Close | 231.40 USD |'].join('\n'));
  assert.equal(find(root, 'table')[0].className, 'kv');
});

test('an ordinary table is not a key/value block', () => {
  const root = document.createElement('div');
  renderMarkdown(root, ['| A | B |', '| --- | --- |', '| 1 | 2 |'].join('\n'));
  assert.equal(find(root, 'table')[0].className, '');
});

// ── diagram layout ───────────────────────────────────────────────────
// Layout is a pure function so it can be tested without a browser. The case
// that matters is the cycle: a model asked to draw a process draws a loop about
// as often as not, and layering a graph with a loop by relaxation never
// settles; each pass pushes the whole cycle one row down.

test('a chain lays out as one node per row', async () => {
  const { layout } = await import('../../web/diagram.js');
  const model = layout(
    [{ id: 'a', label: 'One' }, { id: 'b', label: 'Two' }, { id: 'c', label: 'Three' }],
    [{ from: 'a', to: 'b' }, { from: 'b', to: 'c' }],
  );
  const rows = model.nodes.map((node) => node.y);
  assert.equal(new Set(rows).size, 3);
});

test('siblings share a row', async () => {
  const { layout } = await import('../../web/diagram.js');
  const model = layout(
    [{ id: 'r', label: 'Root' }, { id: 'x', label: 'Left' }, { id: 'y', label: 'Right' }],
    [{ from: 'r', to: 'x' }, { from: 'r', to: 'y' }],
  );
  const [x, y] = ['x', 'y'].map((id) => model.nodes.find((node) => node.id === id));
  assert.equal(x.y, y.y);
  assert.notEqual(x.x, y.x);
});

test('a cycle does not push the graph into a tower', async () => {
  const { layout } = await import('../../web/diagram.js');
  const nodes = ['a', 'b', 'c', 'd'].map((id) => ({ id, label: id.toUpperCase() }));
  const model = layout(nodes, [
    { from: 'a', to: 'b' },
    { from: 'b', to: 'c' },
    { from: 'c', to: 'd' },
    { from: 'd', to: 'a' }, // closes the loop
  ]);
  assert.equal(new Set(model.nodes.map((node) => node.y)).size, 4);
  assert.equal(model.edges.filter((edge) => edge.back).length, 1);
});

test('a back edge is drawn inside the canvas, not off its right side', async () => {
  const { layout } = await import('../../web/diagram.js');
  const model = layout(
    [{ id: 'a', label: 'Start' }, { id: 'b', label: 'End' }],
    [{ from: 'a', to: 'b' }, { from: 'b', to: 'a', label: 'retry' }],
  );
  const back = model.edges.find((edge) => edge.back);
  assert.ok(back.labelX < model.width);
});

test('a long label wraps to two lines and never more', async () => {
  const { layout } = await import('../../web/diagram.js');
  const model = layout(
    [{ id: 'a', label: 'A' }, { id: 'b', label: 'An extremely long label that will not fit' }],
    [{ from: 'a', to: 'b' }],
  );
  const node = model.nodes.find((n) => n.id === 'b');
  assert.ok(node.lines.length <= 2);
  assert.ok(node.w <= 190);
});

// ── math ─────────────────────────────────────────────────────────────
// TeX is what models emit for anything quantitative; rendering it inline is
// the difference between reading a paper and reading its source code.

test('inline math renders as tex, with no raw dollars left over', () => {
  const out = render('Ein Encoder $f$ bildet $x \\in \\mathbb{R}^{d_x}$ auf $h(x)$ ab.');
  assert.ok(find(out, 'span').some((node) => node.className === 'tex'));
  assert.ok(!out.textContent.includes('$'));
  assert.match(out.textContent, /x ∈ ℝ/);
});

test('subscripts and superscripts become elements', () => {
  const out = render('$h = \\sigma(W_e x + b_e)$ und $2^{10}$');
  const found = tags(out);
  assert.ok(found.includes('SUB') && found.includes('SUP'));
});

test('an accent combines with its base character', () => {
  assert.match(render('$\\hat{x}$').textContent, /x̂/);
});

test('currency is not math: no closing delimiter, no rendering', () => {
  const out = render('Das kostet $5 und $10 heute.');
  assert.match(out.textContent, /\$5 und \$10/);
  assert.equal(find(out, 'span').filter((node) => node.className === 'tex').length, 0);
});

test('display math gets its own centred block', () => {
  const out = render('davor\n\n$$\nh = f(x)\n$$\n\ndanach');
  assert.ok(find(out, 'div').some((node) => node.className === 'tex tex-display'));
});

test('LaTeX\'s own delimiters render as math too', () => {
  // Which convention you get is the model's habit, not the reader's business:
  // several models write \( \) and \[ \] by default, and they must not arrive
  // as those literal characters in the middle of a sentence.
  const inline = render('Die Rekonstruktion \\(\\hat{x}\\) folgt aus \\(x\\).');
  assert.match(inline.textContent, /x̂/);
  assert.ok(!inline.textContent.includes('\\('), inline.textContent);

  const display = render('davor\n\n\\[\nh = f(x)\n\\]\n\ndanach');
  assert.ok(find(display, 'div').some((node) => node.className === 'tex tex-display'));
  assert.ok(!display.textContent.includes('\\['), display.textContent);
});

test('a half-written \\( stays literal rather than eating the rest', () => {
  const out = render('Der Encoder \\(f ist eine Abbildung.');
  assert.match(out.textContent, /\\\(f ist eine Abbildung/);
});

test('an unknown command is shown, not swallowed', () => {
  assert.match(render('$x \\frobnicate y$').textContent, /\\frobnicate/);
});

test('a fraction stacks numerator over denominator', () => {
  const out = render('$\\frac{a}{b}$');
  assert.ok(find(out, 'span').some((node) => node.className === 'tex-frac'));
});

test('a half-streamed formula stays literal instead of throwing', () => {
  assert.doesNotThrow(() => render('$h(x) = \\sigma(W_e'));
});

test('a passage citation shows its number, and carries its words to the reader', () => {
  const sources = [{ index: 4, title: 'Attention (PDF p. 4)', url: '/library/f1/4', evidence: ['other words here'] }];
  const out = render('Sie skalieren das Produkt [4: "we scale the dot products by"]. Allgemein [4].', sources);
  const links = find(out, 'A');
  assert.equal(links.length, 2);
  assert.equal(links[0].textContent, '4');
  assert.ok(links[0].classList.contains('passage'));
  assert.match(links[0].href, /\/library\/f1\/4#q=we\+scale\+the\+dot\+products\+by&e=other/);
  // The bare number is a "vgl." reference: the page, no passage of its own.
  assert.ok(!links[1].classList.contains('passage'));
  assert.ok(!out.textContent.includes('we scale'), 'the passage stays out of the prose');
});

test('several passages in one bracket, one holding a bracket of its own, still cite', () => {
  const sources = [{ index: 6, title: 'Log study (PDF p. 2)', url: '/library/f6/2' }];
  const out = render(
    'Logs wachsen schnell [6: "First, the volume of logs is increasing rapidly, which makes the manual method prohibitive.", ' +
      '"Third, logging statements in modern systems updates frequently (e.g., hundreds of new logging statements every month [ 14])."].',
    sources,
  );
  const [link] = find(out, 'A');
  assert.equal(link.textContent, '6');
  assert.ok(link.classList.contains('passage'));
  const hash = new URLSearchParams(link.href.split('#')[1]);
  assert.equal(hash.getAll('q').length, 2);
  assert.match(hash.getAll('q')[1], /every month \[ 14\]\)\.$/);
  assert.equal(out.textContent.trim(), 'Logs wachsen schnell 6.');
});

test('words in quotation marks are left as they are written', () => {
  const out = render('Sie schreiben „we scale the dot products by one over root d“ [1].');
  assert.equal(find(out, 'MARK').length, 0);
  assert.ok(out.textContent.includes('„we scale the dot products by one over root d“'));
});

test('a German term in quotation marks does not swallow the passage citation after it', () => {
  // The answer that broke it: „ paired with the citation's own ", and the
  // sentence between was highlighted with the citation's insides.
  const out = render(
    'Sie schlagen vor, LLM-Werkzeuge als „Fenster“ auch in anderen Fachgebieten zu untersuchen ' +
      '[3: "future research directions for using LLM-based tools as “windows” rather than “walls”"].',
    [{ index: 3, title: 'PlenaMap (PDF p. 5)', url: '/library/f3/5', byline: 'Bodon et al. 2026' }],
  );
  assert.equal(find(out, 'MARK').length, 0);
  assert.equal(
    out.textContent.trim(),
    // German prose, so "S.", whatever the browser's language.
    'Sie schlagen vor, LLM-Werkzeuge als „Fenster“ auch in anderen Fachgebieten zu untersuchen 1, S. 5.',
  );
});

test('a passage holding quotation marks and a citation of its own still cites', () => {
  const sources = [{ index: 4, title: 'Buzz (PDF p. 3)', url: '/library/f4/3' }];
  const out = render(
    'Buzz nutzt MCMC [4: "Since our goal is to detect whether the last data point is an anomaly, we assume it to be ' +
      '"anomaly", and iteratively use MCMC imputation (also used by Donut [4]) to obtain an estimation of x"].',
    sources,
  );
  assert.equal(out.textContent.trim(), 'Buzz nutzt MCMC 4.');
  assert.equal(find(out, 'MARK').length, 0);
  const [link] = find(out, 'A');
  const [quote] = new URLSearchParams(link.href.split('#')[1]).getAll('q');
  assert.match(quote, /^Since our goal .*"anomaly", and .*estimation of x$/);
});

// ── library pages, cited as a thesis cites them ─────────────────────

const PAGE = { index: 4, title: 'Attention (PDF p. 5)', url: '/library/f1/5', byline: 'Vaswani et al. 2017' };

const P = /^de\b/i.test(globalThis.navigator?.language ?? '') ? 'S.' : 'p.';

test('a citation of a library page is its paper and page in the grey pill', () => {
  // Anchored at the words that carry the statement, but not a quotation: the
  // words travel with the link and stay out of the prose.
  const out = render('Sie skalieren das Produkt [4: "we scale the dot products by"]. Allgemein [4].', [PAGE]);
  const [anchored, plain] = find(out, 'A');
  assert.equal(anchored.textContent, `1, ${P} 5`);
  assert.equal(plain.textContent, `1, ${P} 5`);
  assert.ok(anchored.classList.contains('cite'));
  assert.ok(anchored.classList.contains('passage'));
  assert.match(anchored.href, /\/library\/f1\/5#q=we\+scale/);
  assert.match(anchored.getAttribute('aria-label'), /^(vgl\.|cf\.) Vaswani et al\. 2017, (S\.|p\.) 5$/);
  assert.ok(!out.textContent.includes('we scale'), 'the words stay out of the prose');
  assert.equal(find(out, 'MARK').length, 0);
});

test('a direct quotation saved in an older answer keeps its words, unhighlighted', () => {
  const out = render('Sie schreiben, sie [4 quote: "scale the dot products by"].', [PAGE]);
  assert.equal(find(out, 'A')[0].textContent, `1, ${P} 5`);
  assert.equal(find(out, 'MARK').length, 0);
  assert.match(out.textContent, /sie .scale the dot products by. 1, \S+ 5\.$/);
});

test('a source without a byline keeps its number', () => {
  const out = render('Sie skalieren [4: "we scale the dot products by"].', [{ ...PAGE, byline: undefined }]);
  assert.equal(find(out, 'A')[0].textContent, '4');
  assert.ok(!out.textContent.includes('we scale'));
});

test('Copy writes library citations out and leaves web ones alone', () => {
  const text = citedText('A [4 quote: "we scale the dot products by"]. C [4: "other words here"]. B [1].', [
    PAGE,
    SOURCES[0],
  ]);
  assert.match(
    text,
    /^A .we scale the dot products by. \(Vaswani et al\. 2017, (S\.|p\.) 5\)\. C \((vgl\.|cf\.) Vaswani et al\. 2017, (S\.|p\.) 5\)\. B \[1\]\.$/,
  );
});

test('the same page cited twice in a row is one citation carrying both passages', () => {
  const out = render('Text [4: "the first words here"][4: "the second words here"] und mehr.', [PAGE]);
  const links = find(out, 'A');
  assert.equal(links.length, 1);
  assert.equal(new URLSearchParams(links[0].href.split('#')[1]).getAll('q').length, 2);
  assert.match(out.textContent, /^Text 1, \S+ 5 und mehr\.$/);
});

test('pages of one paper share its number, and papers are numbered as first listed', () => {
  const sources = [
    { index: 1, title: 'Window or Wall (PDF p. 28)', url: '/library/a/28', byline: 'Bodon et al. 2026' },
    { index: 2, title: 'Attention (PDF p. 5)', url: '/library/b/5', byline: 'Vaswani et al. 2017' },
    { index: 3, title: 'Window or Wall (PDF p. 3)', url: '/library/a/3', byline: 'Bodon et al. 2026' },
    { index: 4, title: 'A web page', url: 'https://example.org' },
  ];
  assert.deepEqual([...paperNumbers(sources)], [[1, 1], [2, 2], [3, 1]]);
  const out = render('Erstens [3: "some words here"]. Zweitens [2]. Drittens [1]. Web [4].', sources);
  assert.deepEqual(
    find(out, 'A').map((a) => a.textContent),
    [`1, ${P} 3`, `2, ${P} 5`, `1, ${P} 28`, '4'],
  );
});

test('the page is "p." in an English answer and "S." in a German one', () => {
  const page = [{ index: 1, title: 'Window or Wall (PDF p. 33)', url: '/library/a/33', byline: 'Bodon et al. 2026' }];
  const en = render('The authors say that no studies yet look at how novices use these tools [1].', page);
  assert.equal(find(en, 'A')[0].textContent, '1, p. 33');
  const de = render('Die Autoren sagen, dass es noch keine Studien gibt, die das untersuchen [1].', page);
  assert.equal(find(de, 'A')[0].textContent, '1, S. 33');
});

test('two different pages side by side stay two citations', () => {
  const sources = [
    { index: 1, title: 'Window or Wall (PDF p. 28)', url: '/library/a/28', byline: 'Bodon et al. 2026' },
    { index: 3, title: 'Window or Wall (PDF p. 3)', url: '/library/a/3', byline: 'Bodon et al. 2026' },
  ];
  assert.equal(find(render('Text [1][3].', sources), 'A').length, 2);
});
