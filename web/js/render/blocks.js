// One render function per block type, plus a view that reconciles them.
//
// The naive version replaces every block on every patch. That works, but during
// a streamed answer it fires ~8 times a second and destroys any text the user
// has selected, plus every open <details>. So the view keeps a node per block
// id and re-renders only the ones whose data actually changed.

import { iconNode } from '../shared/icons.js';
import { citeLink, outbound, safeUrl, sourceUrl } from './links.js';
import { citedText, paperNumbers, renderMarkdown } from './markdown.js';
import { renderDiagram } from './diagram.js';
import { renderSparkline } from './spark.js';
import { attachPreview, onBrokenImage } from './preview.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

function head(name, label) {
  const node = el('div', 'block-head');
  node.append(iconNode(name, 15), el('span', 'block-head-label', label));
  return node;
}

// A source from the chat's project has no host; it lives here. Falling through
// to the raw URL would print "/#chat=8f2a…" where every other card names a site.
const INTERNAL_PREFIX = '/#chat=';
// A page of a PDF in a literature project's library (app/search/library.py).
const LIBRARY_PREFIX = '/library/';

const hostname = (url) => {
  if (typeof url === 'string' && url.startsWith(INTERNAL_PREFIX)) return 'Earlier in this project';
  if (typeof url === 'string' && url.startsWith(LIBRARY_PREFIX)) return 'Your papers';
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
};

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// A block that has stopped working, whichever way it stopped. What folds away
// and what stays open is decided by this and nothing else, so the research box,
// the plan and each sub-question all agree on when "finished" is.
const isSettled = (status) => status === 'done' || status === 'failed';

// How long a single patch of new prose takes to fade in, at most. Long enough
// that an answer arriving in one piece reads as written rather than pasted,
// short enough that it never becomes the reason you are waiting.
const CASCADE_S = 1.6;

// ── renderers ────────────────────────────────────────────────────────

// Exported for `tests/frontend/blocks.test.mjs`. These run on values a model
// wrote, and the rule that none of them may become markup is exactly the kind
// of thing that is invisible in review and obvious in a test.
export const renderers = {
  // While the run is working the steps are the whole point, so they are shown
  // as they arrive. Once it is finished they are history, and history belongs
  // behind one line the reader can open. The answer should be what the eye
  // lands on, not a list of what it took to get there.
  research(data) {
    const root = el('div', `block research ${data.status ?? ''}`);
    const steps = data.sub_steps ?? [];
    const list = el('ul', 'sub-steps');
    for (const step of steps) {
      const item = el('li', `sub-step ${step.status ?? ''}`);
      // A tick for what is finished, a cross for what failed, a pulsing dot
      // for what is still running: the state reads before the words do.
      const mark = el('span', 'sub-step-mark');
      if (step.status === 'done') mark.append(iconNode('check', 12));
      else if (step.status === 'failed') mark.append(iconNode('close', 12));
      else mark.append(el('span', 'dot'));
      const body = el('div', 'sub-step-body');
      body.append(el('span', 'sub-step-text', step.text));
      if (step.detail) body.append(el('span', 'sub-step-detail', step.detail));
      item.append(mark, body);
      list.append(item);
    }

    if (data.status !== 'done' && data.status !== 'failed') {
      root.append(head('spark', data.title ?? 'Researching'), list);
      return root;
    }

    const details = document.createElement('details');
    details.dataset.status = data.status ?? '';
    const summary = document.createElement('summary');
    summary.append(iconNode('spark', 15), el('span', null, data.title ?? 'Researching'));
    if (steps.length) summary.append(el('span', 'research-count', plural(steps.length, 'step')));
    details.append(summary, list);
    root.append(details);
    return root;
  },

  // Folds away when it is finished, like the research box above it: while the
  // run is planning the list is the news, and afterwards the sub-questions are
  // each shown in full below it anyway.
  plan(data) {
    const root = el('div', `block plan ${data.status ?? ''}`);
    const questions = data.sub_questions ?? [];
    const label = `Plan · ${plural(questions.length, 'sub-question')}`;
    const list = el('ol', 'plan-list');
    for (const question of questions) list.append(el('li', null, question));

    if (data.status !== 'done' && data.status !== 'failed') {
      root.append(head('layers', label), list);
      return root;
    }

    const details = document.createElement('details');
    details.dataset.status = data.status ?? '';
    const summary = document.createElement('summary');
    summary.append(iconNode('layers', 15), el('span', null, label));
    details.append(summary, list);
    root.append(details);
    return root;
  },

  // Open while it is being researched, folded the moment it is not. Five of
  // these at once is a page of findings nobody reads while the report they are
  // for is still being written, and the one that is still working is the one
  // worth seeing, which it cannot be if it is the sixth screenful.
  sub_question(data, sources) {
    const root = el('div', `block sub-question ${data.status ?? ''}`);
    const details = document.createElement('details');
    details.open = !isSettled(data.status);
    details.dataset.status = data.status ?? '';
    const used = (data.sources ?? []).map((index) => sources.find((s) => s.index === index)).filter(Boolean);

    const summary = document.createElement('summary');
    summary.append(iconNode('search', 15), el('span', 'sub-question-text', data.question ?? ''));
    // Answered, the whole block becomes this line: the icon and the numbers it
    // came back with. The question was worth four lines while it was being
    // researched and is worth none once it has been. Its findings are folded
    // in behind it, and the numbers are what connect it to the report.
    if (used.length) {
      const badges = el('span', 'sub-question-badges');
      for (const source of used) badges.append(el('span', 'cite', String(source.index ?? '')));
      summary.append(badges);
    }
    details.append(summary);

    const body = el('div', 'sub-question-body');
    if (data.findings) renderMarkdown(body, data.findings, { sources });
    details.append(body);

    if (used.length) details.append(sourceChips(used));

    root.append(details);
    return root;
  },

  sources(data) {
    const sources = data.sources ?? [];
    const root = el('div', 'block sources');
    const details = document.createElement('details');
    details.open = false;
    const summary = document.createElement('summary');
    summary.append(iconNode('link', 15), el('span', null, plural(sources.length, 'source')));
    // What kind of sources, not just how many: "10 sources" says nothing about
    // whether the answer is checkable, "4 official · 1 peer-reviewed" says most
    // of it.
    const mix = tierMix(sources);
    if (mix) summary.append(el('span', 'source-mix', mix));
    details.append(summary, sourceCards(sources));
    root.append(details);
    return root;
  },

  provenance(data) {
    return provenanceFooter(data);
  },

  // ── widgets ───────────────────────────────────────────────────────
  // Built from a schema the server validated, never from markdown the model
  // had to get right. Every one of them carries the source it came from, so a
  // number in a card is exactly as checkable as a number in the prose.

  figure(data, sources) {
    const root = el('div', 'block widget figure-card');

    const head = el('div', 'figure-head');
    head.append(el('span', 'figure-label', data.label ?? ''));
    if (data.as_of) head.append(el('span', 'figure-asof', `as of ${data.as_of}`));
    root.append(head);

    const value = el('div', 'figure-value');
    value.append(el('span', 'figure-number', data.value ?? ''));
    if (data.unit) value.append(el('span', 'figure-unit', data.unit));
    if (data.change) {
      // The direction is the model's own reading of the sign, and it only ever
      // colours the delta and never the value, which would imply a judgement the
      // sources did not make.
      value.append(el('span', `figure-change ${data.direction ?? ''}`.trim(), data.change));
    }
    root.append(value);

    const rows = (data.rows ?? []).filter((row) => row.label && row.value);
    if (rows.length) {
      const list = el('dl', 'figure-rows');
      for (const row of rows) {
        list.append(el('dt', null, row.label), el('dd', null, row.value));
      }
      root.append(list);
    }

    const cite = sourceLink(data.source, sources);
    if (cite) root.append(cite);
    return root;
  },

  match(data, sources) {
    const root = el('div', 'block widget match-card');

    if (data.competition || data.played_at) {
      const meta = el('div', 'match-meta');
      if (data.competition) meta.append(el('span', null, data.competition));
      if (data.played_at) meta.append(el('span', 'match-date', data.played_at));
      if (data.status && data.status !== 'final') {
        meta.append(el('span', `match-status ${data.status}`, data.status));
      }
      root.append(meta);
    }

    const board = el('div', 'match-board');
    board.append(side(data.home), el('span', 'match-vs', 'vs'), side(data.away));
    root.append(board);

    if (data.note) root.append(el('div', 'match-note', data.note));
    const cite = sourceLink(data.source, sources);
    if (cite) root.append(cite);
    return root;
  },

  weather(data, sources) {
    const root = el('div', 'block widget weather-card');

    const head = el('div', 'weather-head');
    head.append(el('span', 'weather-place', data.location ?? ''));
    if (data.as_of) head.append(el('span', 'weather-asof', data.as_of));
    root.append(head);

    const now = el('div', 'weather-now');
    if (data.condition) now.append(conditionIcon(data.condition, 44, 'weather-icon'));

    const reading = el('div', 'weather-reading');
    const temp = el('div', 'weather-temp');
    temp.append(el('span', 'weather-degrees', data.temperature ?? ''));
    if (data.unit) temp.append(el('span', 'weather-unit', data.unit));
    reading.append(temp);
    if (data.summary) reading.append(el('div', 'weather-summary', data.summary));
    now.append(reading);

    // Only the details a source actually stated; an empty column of dashes
    // reads as data that was looked for and not found.
    const details = [
      ['Feels like', data.feels_like],
      ['Wind', data.wind],
      ['Humidity', data.humidity],
    ].filter(([, value]) => value);
    if (details.length) {
      const list = el('dl', 'weather-details');
      for (const [label, value] of details) list.append(el('dt', null, label), el('dd', null, value));
      now.append(list);
    }
    root.append(now);

    const days = (data.days ?? []).filter((day) => day.day && (day.high || day.low));
    if (days.length) {
      const strip = el('ul', 'weather-days');
      for (const day of days) {
        const item = el('li', 'weather-day');
        item.append(el('span', 'weather-dayname', day.day));
        item.append(day.condition ? conditionIcon(day.condition, 20, 'weather-dayicon') : el('span', 'weather-dayicon'));
        const range = el('span', 'weather-range');
        range.append(el('span', 'weather-high', day.high ?? ''));
        if (day.low) range.append(el('span', 'weather-low', day.low));
        item.append(range);
        if (day.precipitation) item.append(el('span', 'weather-precip', day.precipitation));
        strip.append(item);
      }
      root.append(strip);
    }

    // Weather does not come from a page the run registered, so there is no
    // citation number to show: the service that measured it is the
    // attribution, and it gets named for the same reason every source does.
    if (data.service) {
      const from = el('div', 'widget-source');
      from.append(el('span', 'widget-service', `from ${data.service}`));
      root.append(from);
    }
    return root;
  },

  diagram(data, sources) {
    const root = el('div', 'block widget diagram-card');
    if (data.title) root.append(el('div', 'diagram-title', data.title));

    const svg = renderDiagram(data);
    if (!svg) return el('div', 'block widget diagram-card empty');
    const frame = el('div', 'diagram-frame');
    frame.append(svg);
    root.append(frame);

    if (data.caption) root.append(el('div', 'diagram-caption', data.caption));
    const cite = sourceLink(data.source, sources);
    if (cite) root.append(cite);
    return root;
  },

  image(data, sources) {
    const root = el('div', 'block widget image-card');

    const frame = el('div', 'image-frame');
    const img = proxiedImage(data.url, data.caption || data.title || '');
    onBrokenImage(img, () =>
      frame.replaceChildren(el('div', 'image-missing', 'Image could not be loaded')),
    );
    frame.append(img);
    root.append(frame);

    if (data.caption) root.append(el('div', 'image-caption', data.caption));
    const cite = sourceLink(data.source, sources, data.domain);
    if (cite) root.append(cite);
    return root;
  },

  // A quantity over time: the curve first, because the shape of a series is
  // what the eye reads, then the readings themselves. Both are needed: the
  // curve says "rising", only the table says by how much and from what.
  series(data, sources) {
    const root = el('div', 'block widget series-card');

    const top = el('div', 'series-head');
    top.append(el('span', 'series-label', data.label ?? ''));
    if (data.as_of) top.append(el('span', 'series-asof', `as of ${data.as_of}`));
    root.append(top);

    const points = (data.points ?? []).filter((point) => point.at && point.value);
    if (data.change) {
      // Same rule as the figure card: the direction only ever colours the
      // delta, never the values, which would imply a judgement the sources
      // did not make.
      const line = el('div', 'series-change');
      line.append(el('span', `series-delta ${data.direction ?? ''}`.trim(), data.change));
      if (data.unit) line.append(el('span', 'series-unit', data.unit));
      root.append(line);
    }

    const chart = renderSparkline(points.map((point) => point.value));
    if (chart) {
      const frame = el('div', 'series-chart');
      frame.append(chart);
      root.append(frame);
    }

    const table = el('table', 'series-table');
    const body = el('tbody');
    for (const point of points) {
      const row = el('tr');
      const cell = el('td');
      cell.append(el('span', 'series-value', point.value));
      // The unit belongs to the series, not to each reading. Repeating it in
      // every cell is what stops a column of numbers reading as a column.
      if (data.unit) cell.append(el('span', 'series-unit', data.unit));
      row.append(el('th', null, point.at), cell);
      body.append(row);
    }
    table.append(body);
    const scroller = el('div', 'series-scroll');
    scroller.append(table);
    root.append(scroller);

    const cite = sourceLink(data.source, sources);
    if (cite) root.append(cite);
    return root;
  },

  // Two things on the same attributes. A grid rather than a table so the
  // header can carry the two names at the size they deserve.
  comparison(data, sources) {
    const root = el('div', 'block widget comparison-card');
    if (data.title) root.append(el('div', 'comparison-title', data.title));

    const grid = el('div', 'comparison-grid');
    grid.append(
      el('span', 'comparison-corner'),
      el('span', 'comparison-name', data.left?.name ?? ''),
      el('span', 'comparison-name', data.right?.name ?? ''),
    );
    for (const row of data.rows ?? []) {
      grid.append(
        el('span', 'comparison-attr', row.label ?? ''),
        el('span', 'comparison-cell', row.left ?? ''),
        el('span', 'comparison-cell', row.right ?? ''),
      );
    }
    root.append(grid);

    const cite = sourceLink(data.source, sources);
    if (cite) root.append(cite);
    return root;
  },

  // A procedure. The numbers are the point, so they are rendered rather than
  // written. A model that skips from 2 to 4 cannot do so here.
  steps(data, sources) {
    const root = el('div', 'block widget steps-card');
    if (data.title) root.append(el('div', 'steps-title', data.title));

    const list = el('ol', 'steps-list');
    for (const step of data.steps ?? []) {
      const item = el('li', 'step');
      const body = el('div', 'step-body');
      body.append(el('div', 'step-text', step.text ?? ''));
      if (step.detail) body.append(el('div', 'step-detail', step.detail));
      item.append(el('span', 'step-num'), body);
      list.append(item);
    }
    root.append(list);

    const cite = sourceLink(data.source, sources);
    if (cite) root.append(cite);
    return root;
  },

  // A panel composed per answer from a closed set of blocks. The server chose
  // which blocks and validated every item against the run's sources, so this
  // only lays them out, each block kind has exactly one way to look.
  overview(data, sources) {
    const root = el('div', 'block widget overview-card');
    for (const section of data.sections ?? []) {
      const draw = overviewSections[section.kind];
      if (!draw) continue;
      // The kind goes in an attribute, not a class: `overview-list` and
      // `overview-callout` are already the classes of the elements *inside*
      // a section, and a section wearing them too took on their layout, the
      // callout drew its own title inside the grey box.
      const node = el('section', 'overview-section');
      node.setAttribute('data-kind', section.kind);
      if (section.title) node.append(el('div', 'overview-title', section.title));
      node.append(draw(section, sources));
      root.append(node);
    }
    return root;
  },

  // One person, organisation or place. The portrait is optional by design:
  // a profile without one is still a profile, so the row simply reflows.
  profile(data, sources) {
    const root = el('div', 'block widget profile-card');

    const top = el('div', 'profile-head');
    if (data.image?.url) {
      const portrait = el('div', 'profile-portrait');
      const img = proxiedImage(data.image.url, data.name ?? '');
      // The proxies answer every failure with a transparent pixel so a laid-out
      // widget never reflows, which also means `error` never fires. A 1px
      // image is the signal, and without checking for it the well below would
      // just sit there empty.
      onBrokenImage(img, () => portrait.remove());
      portrait.append(img);
      top.append(portrait);
    }

    const ident = el('div', 'profile-ident');
    ident.append(el('div', 'profile-name', data.name ?? ''));
    if (data.subtitle) ident.append(el('div', 'profile-subtitle', data.subtitle));
    top.append(ident);
    root.append(top);

    const facts = (data.facts ?? []).filter((fact) => fact.label && fact.value);
    if (facts.length) {
      const list = el('dl', 'profile-facts');
      for (const fact of facts) list.append(el('dt', null, fact.label), el('dd', null, fact.value));
      root.append(list);
    }

    const cite = sourceLink(data.source, sources);
    if (cite) root.append(cite);
    return root;
  },

  text(data, sources) {
    const root = el('div', 'block text');
    const prose = el('div', 'prose');
    const content = data.content ?? '';
    renderMarkdown(prose, content, { sources });
    root.append(prose);
    // Copy lives in the turn's tray (`BlockView`), with everything else that
    // is about the answer rather than part of it.
    return root;
  },

  thinking(data) {
    const root = el('div', `block thinking ${data.status ?? ''}`);
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.append(iconNode('thought', 14), el('span', null, 'Thoughts'));
    details.append(summary);
    // Markdown like the answer: reasoning models write lists, bold and code
    // in their thoughts too, and shown raw it read as a page of asterisks.
    const body = el('div', 'thinking-body');
    renderMarkdown(body, data.content ?? '');
    details.append(body);
    root.append(details);
    return root;
  },

  // Every other block in this file describes the evidence. This one describes
  // the *answer*: which of its cited sentences the cited source actually
  // carries. Open by default when something failed the check, because a finding
  // behind a fold is a finding the reader does not have.
  verification(data) {
    // Still running: the eye reads, left to right, for as long as the check
    // takes. It is a real model round-trip over an answer that already looks
    // finished, and saying nothing there is how a page passes for done.
    if (data.status === 'active') {
      const root = el('div', 'block verification verify-inline checking');
      root.append(iconNode('eye', 15), el('span', 'verify-line', 'Checking the answer against its sources'));
      return root;
    }
    // The answer cites nothing at all, so there was nothing to check. Loud,
    // because this is the contract being ignored outright rather than a claim
    // coming out weak, and because rendering no block here would look exactly
    // like a clean answer.
    if (data.status === 'uncited') {
      const root = el('div', 'block verification verify-inline flagged');
      const instead = (data.placeholders ?? [])[0];
      root.append(
        iconNode('alert', 14),
        el(
          'span',
          'verify-line',
          instead
            ? `This answer cites no sources. It wrote “${instead}” instead of a number`
            : 'This answer cites no sources, so nothing in it could be checked',
        ),
      );
      return root;
    }
    // Said rather than swallowed: a block that quietly vanished would leave the
    // reader believing the answer was checked when nobody checked it. Two ways
    // that happens, and the repair differs: a provider that fell over, and a
    // run that spent its budget before reaching the check.
    if (data.status === 'failed' || data.status === 'skipped') {
      const root = el('div', 'block verification verify-inline flagged');
      root.append(
        iconNode('alert', 14),
        el(
          'span',
          'verify-line',
          data.status === 'skipped'
            ? 'No budget left for the citation check. This answer is unchecked.'
            : 'The citation check could not run. This answer is unchecked.',
        ),
      );
      return root;
    }

    const claims = data.claims ?? [];
    // A finding, not merely "not supported": an unchecked claim is a gap in the
    // check and belongs in the summary line, but colouring the block for it
    // would put a warning on every long answer and teach the reader to ignore
    // the one that means something.
    const flagged = claims.filter(
      (claim) => claim.verdict === 'partial' || claim.verdict === 'unsupported',
    );
    // Only in a literature project: sentences that cite none of the papers.
    // Listed rather than judged, a framing line and the model's own knowledge
    // look the same to a parser, and only the reader can tell them apart.
    const uncited = data.uncited ?? [];
    const warn = flagged.length > 0 || uncited.length > 0;
    const root = el('div', `block verification${warn ? ' flagged' : ''}`);

    const details = document.createElement('details');
    details.open = warn;
    const summary = document.createElement('summary');
    const line = uncited.length
      ? `${claims.length ? verifyLine(data) : 'Nothing in this answer cites your papers'} · ${plural(uncited.length, 'sentence')} without a source`
      : verifyLine(data);
    summary.append(iconNode(warn ? 'alert' : 'shield', 14), el('span', 'verify-line', line));
    details.append(summary);

    const body = el('div', 'verify-body');
    // Said once, plainly, and never omitted: this check compares the answer to
    // its sources. A reader who took it for a correctness badge would trust it
    // for something it cannot do.
    body.append(
      el(
        'p',
        'verify-scope',
        'Each cited sentence was checked against the source it cites: whether that source states it, not whether it is true.',
      ),
    );
    // Quotations are the one claim checked without a model: the quoted words
    // are searched for, character by character, in the page they cite.
    const cited = data.passages ?? {};
    if (cited.exact || cited.missing) {
      const parts = [];
      if (cited.exact) parts.push(`${plural(cited.exact, 'cited passage')} found word for word on its page`);
      if (cited.missing) parts.push(`${plural(cited.missing, 'cited passage')} not on the page it names`);
      body.append(el('p', `verify-scope${cited.missing ? ' warn' : ''}`, `${parts.join(', ')}.`));
    }
    const quotes = data.quotes ?? {};
    if (quotes.verbatim || quotes.altered) {
      const parts = [];
      if (quotes.verbatim) parts.push(`${plural(quotes.verbatim, 'quotation')} found word for word in the cited source`);
      if (quotes.altered) parts.push(`${plural(quotes.altered, 'quotation')} not found word for word`);
      body.append(el('p', `verify-scope${quotes.altered ? ' warn' : ''}`, `${parts.join(', ')}.`));
    }
    // Figures, the same way: each number of a claim is looked for in the full
    // text of the sources it cites (`verify.check_numbers`).
    if (data.figures) {
      body.append(
        el(
          'p',
          'verify-scope warn',
          `${plural(data.figures, 'statement')} ${data.figures === 1 ? 'gives a figure' : 'give figures'} that the cited source does not contain.`,
        ),
      );
    }
    for (const claim of claims) body.append(verifyClaim(claim));
    if (uncited.length) {
      body.append(
        el(
          'p',
          'verify-scope warn',
          'These sentences cite none of your papers. A line that frames the answer or says the papers do not cover something is fine; anything else did not come from your sources.',
        ),
      );
      for (const text of uncited) body.append(verifyClaim({ text, verdict: 'uncited', sources: [] }));
    }
    // What the writer marked as its own reasoning, a blockquote in the answer:
    // said, not flagged. It is not from the papers and does not claim to be.
    const own = data.own ?? [];
    if (own.length) {
      body.append(el('p', 'verify-scope', 'The answer’s own reasoning, set apart from what your papers say:'));
      for (const text of own) body.append(verifyClaim({ text, verdict: 'own', sources: [] }));
    }

    details.append(body);
    root.append(details);
    return root;
  },

  notice(data) {
    const root = el('div', `block notice ${data.level ?? 'warn'}`);
    root.append(iconNode(data.level === 'info' ? 'spark' : 'alert', 15), el('span', null, data.text ?? ''));
    return root;
  },
};

/** A remote image, always through this origin.
 *
 *  Proxied like every other remote image here: the page talks to one server,
 *  and the sites an answer merely considered learn nothing about the reader.
 *  The URL is never something a model wrote. The server resolves a source
 *  number to it (see agent/widgets.py). */
function proxiedImage(url, alt) {
  const img = document.createElement('img');
  img.src = `/api/image?url=${encodeURIComponent(url ?? '')}`;
  img.alt = alt ?? '';
  img.loading = 'lazy';
  return img;
}

/** Inline chips, for a list of numbers that has to stay in a line of text. */
/** The icon for a condition, or an empty slot. The vocabulary is closed on the
 *  server, so an unknown value here means the model improvised. Better a gap
 *  than a wrong picture of the sky. */
function conditionIcon(condition, size, className) {
  return iconNode(condition, size, className);
}

/** One team on the scoreboard: flag, name, score.
 *
 *  The flag comes from the ISO code the schema validated, not from anything the
 *  model wrote: an unknown code renders no flag rather than the wrong one. */
function side(team) {
  const node = el('div', 'match-side');
  if (team?.code) {
    const flag = document.createElement('img');
    flag.className = 'match-flag';
    flag.src = `/api/flag/${encodeURIComponent(team.code)}`;
    flag.alt = '';
    flag.width = 28;
    flag.height = 21;
    onBrokenImage(flag, () => flag.remove());
    node.append(flag);
  }
  node.append(el('span', 'match-name', team?.name ?? ''));
  if (team?.score) node.append(el('span', 'match-score', team.score));
  return node;
}

/** A citation per item, because an overview's items come from different pages
 *  and one source line under the card would be a citation for one of them. */
function itemCite(index, sources) {
  // No positional fallback here: an overview item always names its source.
  if (!(sources ?? []).some((s) => s.index === index)) return null;
  return citeLink(index, sources, 'overview-cite');
}

/** Text followed by its citation, the unit every overview block is made of,
 *  with an optional bold lead in front of it. */
function cited(tag, className, text, index, sources, lead = '') {
  const node = el(tag, className);
  if (lead) node.append(el('strong', null, lead));
  node.append(el('span', null, text ?? ''));
  const cite = itemCite(index, sources);
  if (cite) node.append(cite);
  return node;
}

const overviewSections = {
  facts(section, sources) {
    const list = el('dl', 'overview-facts');
    for (const item of section.items ?? []) {
      list.append(el('dt', null, item.label ?? ''), cited('dd', null, item.text, item.source, sources));
    }
    return list;
  },

  // The figure is the tile: it gets the size, and what it measures sits under
  // it the way it does on the figure card.
  stats(section, sources) {
    const row = el('div', 'overview-stats');
    for (const item of section.items ?? []) {
      const tile = el('div', 'overview-stat');
      tile.append(cited('div', 'overview-stat-value', item.text, item.source, sources));
      tile.append(el('div', 'overview-stat-label', item.label ?? ''));
      if (item.note) tile.append(el('div', 'overview-stat-note', item.note));
      row.append(tile);
    }
    return row;
  },

  timeline(section, sources) {
    const list = el('ol', 'overview-timeline');
    for (const item of section.items ?? []) {
      const entry = el('li', 'overview-event');
      entry.append(el('span', 'overview-when', item.label ?? ''));
      entry.append(cited('span', 'overview-what', item.text, item.source, sources));
      list.append(entry);
    }
    return list;
  },

  // Two columns, and the side is carried by the heading and the marker, never
  // by colour alone.
  proscons(section, sources) {
    const grid = el('div', 'overview-proscons');
    for (const [side, heading] of [['pro', 'For'], ['con', 'Against']]) {
      const column = el('div', `overview-side ${side}`);
      column.append(el('div', 'overview-side-head', heading));
      const list = el('ul');
      for (const item of (section.items ?? []).filter((i) => i.label === side)) {
        list.append(cited('li', null, item.text, item.source, sources));
      }
      column.append(list);
      grid.append(column);
    }
    return grid;
  },

  list(section, sources) {
    const list = el('ul', 'overview-list');
    for (const item of section.items ?? []) {
      list.append(cited('li', null, item.text, item.source, sources, item.label));
    }
    return list;
  },

  grid(section, sources) {
    const table = el('table', 'overview-grid');
    const head = el('thead');
    const top = el('tr');
    top.append(el('th'));
    for (const column of section.columns ?? []) top.append(el('th', null, column));
    head.append(top);
    const body = el('tbody');
    for (const item of section.items ?? []) {
      const row = el('tr');
      row.append(cited('th', null, item.label, item.source, sources));
      for (const cell of item.cells ?? []) row.append(el('td', null, cell));
      body.append(row);
    }
    table.append(head, body);
    const scroller = el('div', 'overview-scroll');
    scroller.append(table);
    return scroller;
  },

  callout(section, sources) {
    const [item] = section.items ?? [];
    const node = el('div', 'overview-callout');
    node.append(iconNode('alert', 15));
    node.append(cited('div', 'overview-callout-text', item?.text, item?.source, sources, item?.label));
    return node;
  },
};

/** The "from [3] domain.test" line every widget ends with. A widget without a
 *  resolvable source renders no line. It never gets this far, though, because the
 *  server refuses to emit one. */
function sourceLink(index, sources, fallbackDomain = '') {
  const source = (sources ?? []).find((s) => s.index === index);
  if (!source) return fallbackDomain ? el('div', 'widget-source', fallbackDomain) : null;

  const row = el('div', 'widget-source');
  const link = outbound(sourceUrl(source), 'widget-cite');
  link.dataset.cite = String(index);
  link.classList.add('cite-link');
  link.append(el('span', 'num', String(index)), el('span', null, source.domain || hostname(source.url)));
  attachPreview(link, source);
  row.append(link);
  if (source.published) row.append(el('span', 'widget-date', String(source.published).slice(0, 10)));
  return row;
}

function sourceChips(sources) {
  const list = el('ul', 'source-list');
  for (const source of sources) {
    const item = el('li');
    item.append(anchor(source, 'source-chip', [
      el('span', 'num', String(source.index ?? '')),
      el('span', 'source-title', source.title || hostname(source.url)),
    ]));
    list.append(item);
  }
  return list;
}

/** The full source list: one card per source, all the same size.
 *
 *  Engines truncate titles to their own width and append their own site brand,
 *  so flex-wrapped chips come out ragged and cut mid-word. The titles are
 *  normalised on the server; the card's job is to give every one
 *  of them the same box, clamp to two lines rather than one, and put the
 *  provenance underneath: publisher, what kind of source it is, its date, and
 *  which engine found it. */
function sourceCards(sources) {
  const list = el('ul', 'source-cards');
  // A page of the person's papers carries its paper's number, as its
  // citations in the prose do (`paperNumbers`), not the page's own index.
  const papers = paperNumbers(sources);
  for (const source of sources) {
    const item = el('li');
    const main = el('span', 'card-main');
    main.append(el('span', 'card-title', source.title || hostname(source.url)));
    main.append(cardMeta(source));
    const a = anchor(source, 'source-card', [
      el('span', 'card-num', String(papers.get(source.index) ?? source.index ?? '')),
      main,
    ]);
    a.style.animationDelay = `${Math.min(source.index * 0.03, 0.5)}s`;
    item.append(a);
    list.append(item);
  }
  return list;
}

function cardMeta(source) {
  const meta = el('span', 'card-meta');
  meta.append(el('span', 'card-domain', source.domain || hostname(source.url)));
  if (source.tier && source.tier !== 'web') {
    meta.append(el('span', `tier tier-${source.tier}`, source.tier_label ?? source.tier));
  }
  if (source.published) meta.append(el('span', 'card-date', String(source.published).slice(0, 10)));
  // Which engine found it. The answer to "I am never sure which search engine
  // it used", per source rather than only in the run footer.
  if (source.engines?.length) {
    meta.append(el('span', 'card-engines', source.engines.slice(0, 2).join(', ')));
  }
  if (source.read) meta.append(el('span', 'card-read', 'read'));
  return meta;
}

function anchor(source, className, children) {
  const link = outbound(sourceUrl(source), className);
  // `aria-label`, deliberately not `title`. A `title` renders the browser's own
  // tooltip after its own delay, which then appears *on top of* the hover card
  // saying the same things. Screen
  // readers still get the full text; sighted readers get the card.
  link.setAttribute(
    'aria-label',
    [
      source.title || source.url,
      [source.tier_label, source.domain, source.published?.slice(0, 10)].filter(Boolean).join(' · '),
      source.engines?.length ? `found by ${source.engines.join(', ')}` : '',
    ]
      .filter(Boolean)
      .join('. '),
  );
  link.append(...children);
  return attachPreview(link, source);
}

// Ordered best to worst, and kept in step with `app/search/quality.py`.
const TIER_ORDER = [
  'primary',
  'academic',
  'technical',
  'reference',
  'news',
  'project',
  'web',
  'aggregator',
];
const TIER_WORDS = {
  primary: 'official',
  academic: 'peer-reviewed',
  technical: 'docs',
  reference: 'reference',
  news: 'news',
  // An answer this deployment wrote earlier, retrieved from the chat's project.
  project: 'from this project',
  aggregator: 'aggregated',
};

/** "4 official · 1 peer-reviewed". Plain web sources are the baseline and go
 *  unmentioned, so the summary only ever says something worth reading. */
function tierMix(sources) {
  const counts = new Map();
  for (const source of sources) counts.set(source.tier, (counts.get(source.tier) ?? 0) + 1);
  const parts = TIER_ORDER.filter((tier) => TIER_WORDS[tier] && counts.get(tier)).map(
    (tier) => `${counts.get(tier)} ${TIER_WORDS[tier]}`,
  );
  return parts.length ? parts.join(' · ') : '';
}

// ── the view ─────────────────────────────────────────────────────────

// What the run is doing, for the line that stands in for the answer until the
// answer exists. Named after the phases the backend emits (`app/agent/blocks.py`).
//
// Only the phases that have nothing else on screen. Understanding, searching
// and reading each write their own step into the research box as they happen,
// naming them again underneath it is the same sentence twice, once as a list
// the reader is watching and once as a bar they have to learn to ignore. What
// is left is the genuine gap: the card call and the wait for the first token,
// where the research box has already folded and the page otherwise looks
// finished and wrong.
const PHASE_LABELS = {
  composing: 'Building the answer card',
  writing: 'Writing the answer',
};

// ── the tray ─────────────────────────────────────────────────────────
//
// What a finished answer carries besides itself: the steps it took, its
// sources, the citation check, the run footer, when and by which model. As
// lines of their own that would be five lines of metadata for three of prose,
// so each is an icon in one row under the answer, its summary line is the
// icon's tooltip, and a click opens the block itself below the row, one at a
// time.
//
// The citation check is the one icon that carries its verdict on its face, so
// a finding is never a grey icon among grey icons: orange with the number of
// problems beside it, light green when every cited statement held, grey when
// no verdict exists. While it runs, the eye reads in the icon itself, with a
// word beside it, rather than in a line the icon row sits in front of.

/** The citation check's icon: its tone, the count on its pill, and while it
 *  runs the word beside it. Green only when every statement was checked and
 *  held; a check that left some unchecked has not earned it. */
function verifyState(data) {
  if (data.status === 'active') return { tone: 'checking', icon: 'eye', label: 'Checking' };
  if (data.status === 'uncited') return { tone: 'warn', icon: 'alert' };
  if (data.status === 'failed' || data.status === 'skipped') return { tone: 'off', icon: 'shield' };
  const claims = data.claims ?? [];
  const problems =
    claims.filter((c) => c.verdict === 'partial' || c.verdict === 'unsupported').length +
    (data.uncited ?? []).length;
  if (problems) return { tone: 'warn', icon: 'alert', count: problems };
  const checked = claims.length > 0 && claims.every((c) => c.verdict === 'supported');
  return { tone: checked ? 'ok' : 'off', icon: 'shield' };
}

/** The tooltip of the citation check's icon. */
function verifyTitle(data) {
  if (data.status === 'active') return 'Checking the answer against its sources';
  if (data.status === 'uncited') return 'This answer cites no sources, so nothing in it could be checked';
  if (data.status === 'skipped') return 'No budget left for the citation check. This answer is unchecked.';
  if (data.status === 'failed') return 'The citation check could not run. This answer is unchecked.';
  const uncited = (data.uncited ?? []).length;
  const claims = data.claims ?? [];
  if (!uncited) return verifyLine(data);
  const head = claims.length ? verifyLine(data) : 'Nothing in this answer cites your papers';
  return `${head} · ${plural(uncited, 'sentence')} without a source`;
}

/** Per block kind: its icon, whether it belongs in the tray yet, and the line
 *  that stands in for it as the icon's tooltip. `settled` is the turn's. */
const TRAY = {
  research: {
    icon: 'spark',
    when: (d, settled) => settled || isSettled(d.status),
    title: (d) => {
      const steps = (d.sub_steps ?? []).length;
      return `${d.title ?? 'Researching'}${steps ? ` · ${plural(steps, 'step')}` : ''}`;
    },
  },
  plan: {
    icon: 'layers',
    when: (d, settled) => settled || isSettled(d.status),
    title: (d) => `Plan · ${plural((d.sub_questions ?? []).length, 'sub-question')}`,
  },
  thinking: {
    icon: 'thought',
    when: (d, settled, prose) => settled || prose || d.status === 'done',
    title: () => 'Thoughts',
  },
  sources: {
    icon: 'link',
    when: () => true,
    title: (d) => {
      const sources = d.sources ?? [];
      const mix = tierMix(sources);
      return `${plural(sources.length, 'source')}${mix ? ` · ${mix}` : ''}`;
    },
  },
  verification: {
    icon: 'shield',
    when: () => true,
    title: verifyTitle,
    state: verifyState,
  },
  provenance: {
    icon: 'gauge',
    when: () => true,
    title: (d) => summaryLine(d) || 'How this answer was made',
  },
};

/** The nearest ancestor that scrolls, where the transcript actually moves. */
function scrollParent(node) {
  for (let at = node.parentNode; at && at.nodeType === 1; at = at.parentNode) {
    const { overflowY } = getComputedStyle(at);
    if ((overflowY === 'auto' || overflowY === 'scroll') && at.scrollHeight > at.clientHeight) return at;
  }
  return document.scrollingElement;
}

/** Make `into`'s children look like `from`'s, keeping every node that is
 *  already right.
 *
 *  A streamed answer is re-rendered from its markdown on every patch, and
 *  swapping in the fresh tree wholesale replaced every citation under the
 *  pointer eight times a second: the hovered chip lost `:hover` and got it
 *  back on the next mouse event, flashing between its two colours, and the
 *  hover card was rebuilt each time. Finished paragraphs are identical between
 *  two renders, and in the one still being written only the tail differs, so
 *  matching node by node keeps the citations (and a selection) where they are.
 *
 *  Kept nodes keep their listeners, so a listener must not close over content
 *  that can change without changing the node's attributes or text (the code
 *  block's Copy reads its code from the DOM for that reason). */
function morph(into, from) {
  // The test DOM has no `isEqualNode`; there the old wholesale swap is exact.
  if (typeof into.isEqualNode !== 'function') {
    into.replaceChildren(...from.childNodes);
    return;
  }
  const next = [...from.childNodes];
  next.forEach((node, i) => {
    const old = into.childNodes[i];
    if (!old) into.appendChild(node);
    else if (old.isEqualNode(node)) return;
    else if (old.nodeType === 3 && node.nodeType === 3) old.data = node.data;
    else if (old.nodeType === 1 && old.cloneNode(false).isEqualNode(node.cloneNode(false))) morph(old, node);
    else old.replaceWith(node);
  });
  while (into.childNodes.length > next.length) into.lastChild.remove();
}

/** Put `nodes` into `parent` in this order, moving only what is out of place:
 *  a node detached and put back restarts its animations and loses a selection. */
function place(parent, nodes) {
  for (let i = 0; i < nodes.length; i++) {
    const node = nodes[i];
    const at = parent.childNodes[i];
    if (at === node) continue;
    if (at) parent.insertBefore(node, at);
    else parent.appendChild(node);
  }
  while (parent.childNodes.length > nodes.length) parent.lastChild.remove();
}

export class BlockView {
  constructor(container) {
    this.container = container;
    this.nodes = new Map(); // block id -> { node, key }
    this.textCounts = new Map(); // block id -> rendered character count so far
    this.phase = '';
    this.hasProse = false;
    this.workNode = null;
    this.blocks = [];
    this.settled = false;
    // The tray: one row of icons, and below it the block one of them opened.
    this.tray = el('div', 'turn-tray');
    this.trayIcons = el('div', 'tray-icons');
    this.trayPanel = el('div', 'tray-panel');
    this.tray.append(this.trayIcons, this.trayPanel);
    this.chips = new Map(); // chip id -> button
    this.openChip = null;
    this.copyText = '';
    this.meta = null; // { text, title } from `setMeta`
    this.metaNode = null;
  }

  /** The coarse state of the run, from the `phase` events.
   *
   * Between the last search step and the first word of the answer the run can
   * sit for half a minute (a widget call and a first token both take real
   * time), and without this line the page would simply look finished and
   * wrong. It says which of those is happening, and it goes away by itself the
   * moment the prose starts, because words appearing on screen are a better
   * progress indicator than any label. */
  setPhase(phase) {
    this.phase = phase ?? '';
    this.#syncWork();
  }

  render(blocks) {
    this.blocks = blocks;
    const sources = blocks.find((b) => b.type === 'sources')?.data?.sources ?? [];
    // Citation numbers resolve against the source list, so a text block has to
    // re-render when sources arrive even though its own data has not changed.
    const sourceKey = sources.length;
    this.hasProse = blocks.some((b) => b.type === 'text' && (b.data?.content ?? '').length > 0);

    // A sub-question is the plan in longhand: once the plan has gone to the
    // tray, the finished ones open with it rather than standing in the answer.
    const plan = blocks.find((b) => b.type === 'plan');
    const planChip = plan && TRAY.plan.when(plan.data ?? {}, this.settled) ? plan.id : null;

    const main = [];
    const tray = []; // { block, node, chip }
    for (const block of blocks) {
      const key = `${JSON.stringify(block.data)}|${sourceKey}`;
      const existing = this.nodes.get(block.id);
      let node = existing?.node;
      if (!existing || existing.key !== key) {
        node = this.#build(block, sources, existing?.node);
        this.nodes.set(block.id, { node, key });
      }
      const data = block.data ?? {};
      const spec = TRAY[block.type];
      if (spec?.when(data, this.settled, this.hasProse)) {
        tray.push({ block, node, chip: block.id });
      } else if (block.type === 'sub_question' && planChip && isSettled(data.status)) {
        tray.push({ block, node, chip: planChip });
      } else {
        main.push(node);
      }
    }

    place(this.container, main);
    for (const node of main) node.hidden = false;
    const text = [...blocks].reverse().find((b) => b.type === 'text' && (b.data?.content ?? '').trim());
    this.copyText = text ? citedText(text.data.content, sources) : '';
    this.#syncTray(tray);
    this.#syncWork();
    this.#relink(sources);
  }

  /** When the answer was written and by which model, for the tray's last icon. */
  setMeta(meta) {
    this.meta = meta?.text ? meta : null;
    this.#syncTray();
  }

  /** A line about the turn as a whole (a failed or stopped run): under the
   *  answer, above the tray, so it is read rather than clicked for. */
  note(node) {
    this.#mountTray();
    if (this.tray.parentNode) this.tray.before(node);
    else this.container.append(node);
  }

  #mountTray() {
    const host = this.container.parentNode;
    if (host && this.tray.parentNode !== host) this.container.after(this.tray);
  }

  #syncTray(tray = this.trayItems ?? []) {
    this.trayItems = tray;
    this.#mountTray();

    const chips = [];
    // Copy first, and only once the answer is finished: copying half a
    // sentence is not a thing anyone wants, and a button offering it while the
    // words are still arriving reads as "this is done" when it is not.
    if (this.settled && this.copyText) chips.push(this.#chip('#copy', 'copy', 'Copy the answer'));
    const seen = new Set();
    for (const { block, chip } of tray) {
      if (chip !== block.id || seen.has(chip)) continue;
      seen.add(chip);
      const spec = TRAY[block.type];
      const data = block.data ?? {};
      const state = spec.state?.(data);
      chips.push(this.#chip(chip, state?.icon ?? spec.icon, spec.title(data), undefined, state));
    }
    if (this.meta) chips.push(this.#chip('#meta', 'clock', this.meta.text, this.meta.title));
    const ids = new Set(chips.map((chip) => chip.dataset.chip));
    for (const id of [...this.chips.keys()]) if (!ids.has(id)) this.chips.delete(id);
    if (this.openChip && !ids.has(this.openChip)) this.openChip = null;
    place(this.trayIcons, chips);

    const panel = tray.map(({ node }) => node);
    if (this.meta) {
      this.metaNode ??= el('div', 'block tray-meta');
      this.metaNode.textContent = this.meta.title || this.meta.text;
      panel.push(this.metaNode);
    }
    place(this.trayPanel, panel);
    for (const { node, chip } of tray) {
      node.hidden = chip !== this.openChip;
      // The icon is the fold, so what it opens is open: the summary line is
      // the icon's tooltip.
      const details = node.querySelector(':scope > details');
      if (details) details.open = true;
    }
    if (this.metaNode) this.metaNode.hidden = this.openChip !== '#meta';

    for (const [id, button] of this.chips) {
      const open = id === this.openChip;
      button.classList.toggle('open', open);
      button.setAttribute('aria-expanded', String(open));
    }
    this.tray.hidden = !chips.length;
    this.trayPanel.hidden = !this.openChip;
  }

  #chip(id, icon, label, title = label, state = null) {
    let button = this.chips.get(id);
    if (!button) {
      button = el('button', 'tray-chip');
      button.type = 'button';
      button.dataset.chip = id;
      button.append(iconNode(icon, 15));
      button.addEventListener('click', () => this.#press(id));
      this.chips.set(id, button);
    }
    if (state) this.#paint(button, icon, state);
    button.title = title;
    button.setAttribute('aria-label', label);
    return button;
  }

  /** A chip that carries its block's state: tone, pill, word. Rebuilt only
   *  when the state changes, so the eye's animation is not restarted by every
   *  patch that arrives while the check runs. */
  #paint(button, icon, { tone, count = 0, label = '' }) {
    const key = `${icon}|${tone}|${count}|${label}`;
    if (button.dataset.state === key) return;
    button.dataset.state = key;
    for (const name of ['checking', 'ok', 'warn', 'off']) button.classList.toggle(name, name === tone);
    button.classList.add('stateful');
    const parts = [iconNode(icon, 15)];
    if (label) parts.push(el('span', 'tray-chip-label', label));
    if (count) parts.push(el('span', 'tray-count', String(count)));
    button.replaceChildren(...parts);
  }

  #press(id) {
    if (id === '#copy') {
      const button = this.chips.get(id);
      navigator.clipboard?.writeText(this.copyText);
      button.replaceChildren(iconNode('check', 15));
      button.title = 'Copied';
      setTimeout(() => {
        button.replaceChildren(iconNode('copy', 15));
        button.title = 'Copy the answer';
      }, 1200);
      return;
    }
    this.openChip = this.openChip === id ? null : id;
    this.#syncTray();
    if (this.openChip) globalThis.requestAnimationFrame?.(() => this.#reveal());
  }

  /** Bring an opened tray panel into view.
   *
   *  The tray sits at the very bottom of an answer, which is usually the
   *  bottom of the window, so what an icon opens landed below the fold and
   *  had to be scrolled to. Scroll only as far as the panel needs, and never
   *  so far that the row of icons leaves the top: that row is how the panel
   *  gets closed again, and a tall panel is read from its start. */
  #reveal() {
    const scroller = scrollParent(this.tray);
    if (!scroller || this.trayPanel.hidden) return;
    const view = scroller.getBoundingClientRect();
    const icons = this.trayIcons.getBoundingClientRect();
    const panel = this.trayPanel.getBoundingClientRect();
    const MARGIN = 16;
    const below = panel.bottom + MARGIN - view.bottom;
    if (below <= 0) return;
    const room = icons.top - MARGIN - view.top;
    const by = Math.min(below, Math.max(0, room));
    if (by <= 0) return;
    const smooth = !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    scroller.scrollBy({ top: by, behavior: smooth ? 'smooth' : 'auto' });
  }

  /** Point every citation at what its source now carries.
   *
   *  A page of the person's papers learns what to highlight after the answer
   *  is written: the passages just before it, the checked evidence only once
   *  the citation check has run. Rebuilding the prose for that would cost the
   *  reader their selection mid-read, so the links are updated in place. */
  #relink(sources) {
    const key = sources
      .map((s) => `${s.index}:${s.evidence?.length ?? 0}:${s.passages?.length ?? 0}`)
      .join(',');
    if (key === this.linkKey) return;
    this.linkKey = key;
    for (const link of this.container.querySelectorAll('.cite-link')) {
      const source = sources.find((s) => String(s.index) === link.dataset.cite);
      const quotes = link.dataset.quotes ? JSON.parse(link.dataset.quotes) : [];
      const url = source && safeUrl(sourceUrl(source, quotes));
      if (url) link.href = url;
    }
  }

  /** Keep the working line in step with the phase, as a sibling of the block
   *  list rather than a member of it: `render` reconciles its container against
   *  the run's blocks exactly, and anything else living in there would be
   *  removed as a stray on the next patch. */
  #syncWork() {
    const label = this.hasProse ? '' : (PHASE_LABELS[this.phase] ?? '');
    if (!label) {
      this.workNode?.remove();
      this.workNode = null;
      return;
    }
    if (!this.workNode) {
      this.workNode = el('div', 'working');
      // The phase is named for assistive technology only. On screen it is a
      // bar: at this point in a run there is exactly one thing worth saying
      // (still working), and a sentence that says it is a sentence to read.
      this.workNode.setAttribute('role', 'status');
      this.workNode.append(el('span', 'working-bar'));
    }
    this.workNode.setAttribute('aria-label', label);
    const host = this.container.parentNode;
    if (host && this.workNode.parentNode !== host) host.append(this.workNode);
  }

  #build(block, sources, previous) {
    const render = renderers[block.type];
    const node = render
      ? render(block.data ?? {}, sources)
      : el('div', 'block unknown', `Unsupported block: ${block.type}`);
      
    // Only animate when the block is first added to the page
    if (!previous) node.classList.add('new');
      
    if (previous) {
      // Whether the reader opened or closed a <details> is theirs to decide,
      // until the block changes state. A sub-question that has just finished
      // has to fold even though it was open a patch ago, and carrying the flag
      // across that transition is what kept it open for the rest of the run.
      const before = previous.querySelector(':scope > details');
      const after = node.querySelector(':scope > details');
      if (before && after && before.dataset.status === after.dataset.status) {
        after.open = before.open;
      }
      previous.className = node.className;
      
      // For text blocks, only update the markdown content itself.
      // This prevents the outer container and the Copy button from flickering.
      const prevProse = previous.querySelector('.prose');
      const nextProse = node.querySelector('.prose');
      if (prevProse && nextProse && previous.className === node.className) {
        // Wrap only the freshly streamed words in fade-in spans, so text that
        // is already on screen stays perfectly still while new words fade in.
        const shown = this.textCounts.get(block.id) ?? 0;
        this.textCounts.set(block.id, fadeInNewWords(nextProse, shown));
        morph(prevProse, nextProse);
        return previous;
      }

      // Streamed thoughts: the same <details> stays on the page and only its
      // body is brought up to date. Swapping in a fresh one on every token
      // swallowed clicks on "Thoughts" (mousedown on one summary, mouseup on
      // its replacement is no click), and an opened box jumped back to its top
      // and dropped a selection eight times a second. A reader at the bottom of
      // the box keeps following the newest thought; one who scrolled up stays.
      const prevThought = before?.querySelector(':scope > .thinking-body');
      const nextThought = after?.querySelector(':scope > .thinking-body');
      if (prevThought && nextThought) {
        const following = prevThought.scrollHeight - prevThought.scrollTop - prevThought.clientHeight < 24;
        morph(prevThought, nextThought);
        if (following) prevThought.scrollTop = prevThought.scrollHeight;
        return previous;
      }

      previous.replaceChildren(...node.childNodes);
      return previous;
    }

    return node;
  }

  /** The run is over: everything that was working folds away.
   *
   *  Each block already folds itself when its own status turns, so by the time
   *  this runs most of them are shut. It is the exceptions this is for (a box
   *  the reader opened while they were waiting, a sub-question the run never
   *  finished, a transcript reloaded from history), and the rule is the same
   *  for all of them: when there is an answer, the answer is the page. */
  settle() {
    this.settled = true;
    this.setPhase('');
    // Everything that was still working goes to the tray now, whatever its
    // own status says: a run that stopped mid-step has no steps left to watch.
    this.render(this.blocks);
    this.container.querySelectorAll('.block').forEach((node) => node.classList.add('settled'));
    this.container.querySelectorAll('.block > details').forEach((node) => {
      node.open = false;
    });
  }

  clear() {
    this.setPhase('');
    this.nodes.clear();
    this.textCounts.clear();
    this.blocks = [];
    this.settled = false;
    this.openChip = null;
    this.container.replaceChildren();
    this.#syncTray([]);
  }
}

/**
 * Wraps every freshly streamed word of a re-rendered prose element in a
 * fade-in span. `shown` is how many rendered characters were already on
 * screen from the previous patch. Everything before that offset stays plain,
 * so existing text never re-animates. Returns the new total character count.
 */
function fadeInNewWords(prose, shown) {
  const walker = document.createTreeWalker(prose, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);

  let offset = 0;
  const words = [];
  for (const textNode of textNodes) {
    const text = textNode.nodeValue;
    const start = offset;
    offset += text.length;
    if (offset <= shown) continue; // fully on screen already

    const plain = text.slice(0, Math.max(0, shown - start));
    const fresh = text.slice(plain.length);
    const frag = document.createDocumentFragment();
    if (plain) frag.append(document.createTextNode(plain));
    for (const token of fresh.split(/(\s+)/)) {
      if (!token) continue;
      if (/^\s+$/.test(token)) {
        frag.append(document.createTextNode(token)); // whitespace stays as-is
        continue;
      }
      const span = el('span', 'fade-word', token);
      frag.append(span);
      words.push(span);
    }
    textNode.replaceWith(frag);
  }

  // The stagger is per patch, and the size of a patch is not ours to choose:
  // some providers stream token by token, others hand over the finished answer
  // in one chunk. A fixed per-word delay serves the first and lets the second
  // land as a wall of text, so the step shrinks to fit: a burst cascades over
  // CASCADE_S however large it is, while a three-word patch from a real stream
  // keeps the small step and stays immediate. Nothing is withheld: every word
  // is in the document already, only its opacity is on a clock.
  const step = Math.min(0.025, CASCADE_S / Math.max(words.length, 1));
  words.forEach((span, index) => {
    span.style.animationDelay = `${(index * step).toFixed(3)}s`;
  });
  return offset;
}

export function errorNote(message, { level = 'error' } = {}) {  const node = el('div', `error-note ${level}`);
  node.append(iconNode(level === 'error' ? 'alert' : 'stop', 15), el('span', null, message));
  return node;
}

// ── the run footer ───────────────────────────────────────────────────
//
// Every run ends with one of these, including the ones that failed. It answers
// "what actually happened" without leaving the answer: which engines contributed
// and which refused, which corpora were searched, how many sources survived, and
// what it cost. It is a block rather than a line derived from the terminal event
// so that it is still there when the chat is reopened tomorrow.

const VERDICT_LABELS = {
  supported: 'Supported',
  partial: 'Partly',
  unsupported: 'Not supported',
  unchecked: 'Not checked',
  uncited: 'No source',
  own: 'Own reasoning',
};

/** The one line a reader who does not open the block will see.
 *
 *  It has to be true of the *answer*, not of the subset the check got through.
 *  `total` counts every cited sentence and `checked` only those a verdict came
 *  back for; while they were one number, an answer with thirty-eight claims and
 *  a fourteen-claim limit was summarised as "all 14 cited statements supported",
 *  which every reader took to mean the answer had come through clean.
 *
 *  `total` is absent from runs stored before it existed, and falling back to
 *  `checked` renders those exactly as they were rendered then. */
function verifyLine({ total = 0, checked = 0, supported = 0, partial = 0, unsupported = 0 }) {
  const cited = total || checked;
  if (!cited) return 'No cited statements to check';

  const problems = [];
  if (unsupported) problems.push(`${unsupported} not supported`);
  if (partial) problems.push(`${partial} only partly`);

  if (checked >= cited) {
    if (!problems.length) return `All ${plural(cited, 'cited statement')} supported by their sources`;
    return `${supported} of ${cited} cited statements supported; ${problems.join(', ')}`;
  }
  // Part of the answer never reached a verdict, and the line says so first,
  // the count that is missing is the one a reader would otherwise assume.
  if (!checked) return `None of the ${plural(cited, 'cited statement')} could be checked`;
  const head = `${checked} of ${plural(cited, 'cited statement')} checked`;
  return problems.length ? `${head}; ${problems.join(', ')}` : `${head}, all supported`;
}

function verifyClaim(claim) {
  const row = el('div', `verify-claim ${claim.verdict ?? 'unchecked'}`);
  row.append(el('span', 'verify-chip', VERDICT_LABELS[claim.verdict] ?? claim.verdict ?? ''));

  const text = el('div', 'verify-text');
  text.append(el('span', 'verify-quote', claim.text ?? ''));
  const cites = (claim.sources ?? []).map((number) => `[${number}]`).join(' ');
  if (cites) text.append(el('span', 'verify-cites', cites));
  if (claim.quote === 'verbatim') text.append(el('span', 'verify-quoted', 'quotation verified word for word'));
  if (claim.passage === 'exact') text.append(el('span', 'verify-quoted', 'passage found on the page'));
  if (claim.figures?.length) {
    text.append(el('span', 'verify-figures', `not in the source: ${claim.figures.join(', ')}`));
  }
  // A sentence that carried no number of its own, checked against the next
  // one's (literature projects, `verify.paragraph_claims`).
  if (claim.inherited) text.append(el('span', 'verify-cites', 'source of the next sentence'));
  // Only ever present on a claim that did not pass, which is the whole reason
  // the model is asked for it.
  if (claim.note) text.append(el('div', 'verify-note', claim.note));

  row.append(text);
  return row;
}

function provenanceFooter(data) {
  const search = data.search ?? {};
  const sources = data.sources ?? {};
  const stats = data.stats ?? {};
  const engines = search.engines ?? [];

  const root = el('div', 'block provenance');
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.append(iconNode('search', 13), el('span', 'prov-line', summaryLine(data)));
  details.append(summary);

  const body = el('div', 'prov-body');
  if (engines.length) {
    body.append(
      provRow(
        'Engines',
        engines.map((engine) => `${engine.name} (${engine.results})`).join(' · '),
      ),
    );
  }
  // The engines that refused are the more interesting half on a self-hosted
  // instance: they are why an answer is thinner than it looks.
  const refused = search.unresponsive ?? [];
  if (refused.length) {
    body.append(
      provRow(
        'No answer from',
        refused.map((engine) => `${engine.name}: ${engine.reason}`).join(' · '),
        'warn',
      ),
    );
  }
  if (search.failures?.length) body.append(provRow('Failed searches', search.failures.join(' · '), 'warn'));

  // What was asked for, next to what ran. These differ whenever the router
  // trimmed the work, and a run that quietly did less than it was asked to is
  // the one thing that would make routing untrustable.
  if (data.route_reason) body.append(provRow('Routing', data.route_reason));

  const lenses = (data.lenses ?? []).map((lens) => lens.label).join(', ');
  // Who chose them matters as much as which: a run that searched Science
  // because the model decided to is a different claim from one the reader asked
  // for, and only the footer can say which happened.
  // Only for a run that searched. The corpora are chosen before the router
  // decides, so a question answered from the model alone, or from a picture,
  // still carries them, and listing them claimed a search that never ran.
  if (lenses && search.queries) {
    body.append(provRow('Sources searched', data.lenses_auto ? `${lenses} · chosen automatically` : lenses));
  }
  if (search.queries) body.append(provRow('Queries', String(search.queries)));
  // Two different claims, and only one of them is a restriction: a narrow window
  // excludes older sources at the engine, a preference only ranks recent ones
  // higher. Printing the second as the first describes a search that did not run.
  if (search.time_range) body.append(provRow('Restricted to', `the past ${search.time_range}`));
  else if (data.prefer_recent) body.append(provRow('Ranking', 'recent sources first'));
  if (sources.total) body.append(provRow('Sources kept', sourcesLine(sources)));
  const models = data.models ?? {};
  if (models.writer) {
    const same = !models.judge || models.judge === models.writer;
    body.append(
      provRow(
        'Models',
        same ? `${models.writer} · wrote and checked the answer` : `${models.writer} wrote · ${models.judge} checked`,
      ),
    );
  }
  if (stats.total_tokens) body.append(provRow('Model tokens', tokenLine(stats)));
  // A meter of its own, never added to the one above. Ranking embeds every
  // source once per sub-question, so a deep run spends hundreds of thousands of
  // embedding tokens against a few thousand from the models. Summed, the total
  // makes the price look impossible and the bill look wrong.
  if (stats.embedding?.total_tokens) {
    body.append(provRow('Embedding tokens', embeddingLine(stats.embedding)));
  }
  body.append(provRow('Cost', costLine(stats)));

  details.append(body);
  root.append(details);
  return root;
}

function summaryLine(data) {
  const search = data.search ?? {};
  const sources = data.sources ?? {};
  const stats = data.stats ?? {};
  const bits = [];

  // "Auto → Simple" rather than just "Simple": the reader picked the first and
  // is owed the second.
  if (data.depth_label) {
    const asked = data.requested_depth_label;
    bits.push(asked && asked !== data.depth_label ? `${asked} → ${data.depth_label}` : data.depth_label);
  }
  if (data.mode === 'lookup') {
    bits.push('fetched, not searched');
  } else if (!search.queries) {
    bits.push('answered without searching');
  } else {
    const names = (search.engines ?? []).map((engine) => engine.name);
    const shown = names.slice(0, 3).join(', ');
    const more = names.length - Math.min(names.length, 3);
    if (shown) bits.push(more > 0 ? `${shown} +${more}` : shown);
    else bits.push('no engine answered');
  }
  if (sources.total) {
    bits.push(sources.read ? `${plural(sources.total, 'source')}, ${sources.read} read` : plural(sources.total, 'source'));
  }
  if (stats.elapsed_s != null) bits.push(`${Math.round(stats.elapsed_s)}s`);
  // What the run spent, on the line the reader sees without opening anything,
  // a research app that hides its bill is asking to be trusted about it.
  if (stats.total_tokens) bits.push(`${compactTokens(stats.total_tokens)} tokens`);
  const money = formatCost(stats.cost_usd);
  if (money) bits.push(money);
  return bits.join(' · ');
}

/** 1234 → "1.2k". Exact counts belong in the expanded rows, not the one-liner. */
function compactTokens(count) {
  if (count < 1000) return String(count);
  const thousands = count / 1000;
  return `${thousands < 100 ? thousands.toFixed(1) : Math.round(thousands)}k`;
}

/** A price with enough digits to be true. Fractions of a cent are the normal
 *  case here, and rounding them to "$0.00" would read as free. */
function formatCost(cost) {
  if (cost == null) return ''; // nobody quoted a price: a local model
  if (cost === 0) return 'free';
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(cost < 1 ? 3 : 2)}`;
}

function tokenLine(stats) {
  const bits = [`${stats.prompt_tokens ?? 0} in`, `${stats.completion_tokens ?? 0} out`];
  // Reasoning tokens are billed and invisible: they are most of what a
  // thinking model charges for, and most of why it took as long as it did.
  if (stats.reasoning_tokens) bits.push(`${stats.reasoning_tokens} of them reasoning`);
  return `${stats.total_tokens} total: ${bits.join(', ')}`;
}

/** The ranking meter. Says what it is charged for, because the number is large
 *  next to the model's and the reason is not obvious: every source is embedded
 *  again for every sub-question that ranks it. */
function embeddingLine(embedding) {
  const money = formatCost(embedding.cost_usd);
  const price = money ? (money === 'free' ? ', free' : `, ${money}`) : '';
  return `${embedding.total_tokens} embedded for ranking${price}`;
}

function sourcesLine(sources) {
  const tiers = Object.entries(sources.tiers ?? {})
    .sort((a, b) => TIER_ORDER.indexOf(a[0]) - TIER_ORDER.indexOf(b[0]))
    .map(([tier, count]) => `${count} ${TIER_WORDS[tier] ?? tier}`);
  const capped = sources.limit && sources.total >= sources.limit ? ` · capped at ${sources.limit}` : '';
  return `${sources.total} of them, ${sources.read} read in full${tiers.length ? `; ${tiers.join(', ')}` : ''}${capped}`;
}

function costLine(stats) {
  const bits = [];
  if (stats.elapsed_s != null) bits.push(`${Math.round(stats.elapsed_s)}s`);
  if (stats.searches) bits.push(stats.searches === 1 ? '1 search' : `${stats.searches} searches`);
  if (stats.pages_fetched) bits.push(`${stats.pages_fetched} pages read`);
  if (stats.llm_calls) bits.push(`${stats.llm_calls} model calls`);
  const money = formatCost(stats.cost_usd);
  if (money) bits.push(money === 'free' ? 'free' : `${money} at the provider`);
  return bits.join(' · ') || '—';
}

function provRow(label, value, level = '') {
  const row = el('div', `prov-row ${level}`.trim());
  row.append(el('span', 'prov-key', label), el('span', 'prov-value', value));
  return row;
}
