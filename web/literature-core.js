// The literature page's pure half: what is counted, filtered, labelled and
// summarised, with no DOM. Kept apart so `node --test` can check it without a
// browser, the same split as redact.js and redact-view.js.

export const FILTERS = [
  { id: 'open', label: 'To screen' },
  { id: 'relevant', label: 'Relevant' },
  { id: 'maybe', label: 'Maybe' },
  { id: 'excluded', label: 'Excluded' },
  { id: 'all', label: 'All' },
];

export const DECISIONS = [
  { id: 'relevant', label: 'Relevant', key: '1', icon: 'check' },
  { id: 'maybe', label: 'Maybe', key: '2', icon: 'help' },
  { id: 'excluded', label: 'Exclude', key: '3', icon: 'close' },
];

const SOURCE_LABELS = {
  openalex: 'OpenAlex',
  semantic_scholar: 'Semantic Scholar',
  crossref: 'Crossref',
  arxiv: 'arXiv',
  ieee: 'IEEE Xplore',
};

const KIND_LABELS = {
  journal: 'Journal',
  conference: 'Conference',
  preprint: 'Preprint',
  book: 'Book',
  thesis: 'Thesis',
};

/** How many papers sit under each filter. */
export function tally(papers) {
  const counts = { open: 0, relevant: 0, maybe: 0, excluded: 0, all: papers.length };
  for (const paper of papers) {
    const key = paper.decision || 'open';
    if (key in counts) counts[key] += 1;
  }
  return counts;
}

/** The papers a filter shows, in the order the server ranked them. */
export function visible(papers, filter) {
  if (filter === 'all') return papers;
  if (filter === 'open') return papers.filter((p) => !p.decision);
  return papers.filter((p) => p.decision === filter);
}

/** `Vaswani, Shazeer, Parmar et al.`, surnames only past the first. */
export function authorLine(authors, shown = 3) {
  const list = (authors ?? []).filter(Boolean);
  if (!list.length) return '';
  const names = list.slice(0, shown).map((name) => surname(name));
  return list.length > shown ? `${names.join(', ')} et al.` : names.join(', ');
}

function surname(name) {
  const text = String(name).trim();
  if (text.includes(',')) return text.split(',')[0].trim();
  const parts = text.split(/\s+/);
  return parts[parts.length - 1];
}

/** `2021 · IEEE Transactions on … · Journal · 312 citations` */
export function metaLine(data) {
  const bits = [];
  if (data.year) bits.push(String(data.year));
  if (data.venue) bits.push(data.venue);
  if (KIND_LABELS[data.kind]) bits.push(KIND_LABELS[data.kind]);
  if (Number.isFinite(data.citations) && data.citations > 0) {
    bits.push(`${data.citations.toLocaleString('en')} citation${data.citations === 1 ? '' : 's'}`);
  }
  return bits.join(' · ');
}

export function sourceLine(sources) {
  return (sources ?? []).map((s) => SOURCE_LABELS[s] ?? s).join(' · ');
}

/** Why a paper is here when it was not a plain search hit. Read from the
 *  stored `links`, the number of relevant papers it is tied to. */
export function originLabel(paper) {
  const links = paper.data?.links ?? 0;
  const of = links === 1 ? '1 of your relevant papers' : `${links} of your relevant papers`;
  switch (paper.origin) {
    case 'recommended':
      return 'Recommended from your picks';
    case 'references':
      return links ? `Cited by ${of}` : 'Cited by your relevant papers';
    case 'citations':
      return links ? `Cites ${of}` : 'Cites your relevant papers';
    case 'upload':
      return 'Added by you';
    default:
      return '';
  }
}

/** The key a screening shortcut means, or null. Letters are matched without
 *  modifiers only, so the browser's own shortcuts keep working. */
export function keyAction(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return null;
  switch (event.key) {
    case 'j':
    case 'ArrowDown':
      return 'next';
    case 'k':
    case 'ArrowUp':
      return 'prev';
    case '1':
      return 'relevant';
    case '2':
      return 'maybe';
    case '3':
      return 'excluded';
    case 'a':
      return 'abstract';
    case 'o':
      return 'open';
    case 'u':
      return 'undo';
    default:
      return null;
  }
}

/** Which card takes focus after `id` is decided and, under the current
 *  filter, leaves the list: the one after it, else the one before. */
export function focusAfter(list, id) {
  const index = list.findIndex((p) => p.id === id);
  if (index === -1) return list[0]?.id ?? null;
  return list[index + 1]?.id ?? list[index - 1]?.id ?? null;
}

/** Papers still waiting for a summary, the ones on screen first, then the
 *  rest of the undecided ones. A summary nobody will look at today is a
 *  model call that can wait. */
export function pendingSummaries(papers, filter, failed = new Set()) {
  const wants = (p) => !p.summary && !failed.has(p.id);
  const shown = visible(papers, filter).filter(wants);
  const rest = papers.filter((p) => !p.decision && wants(p) && !shown.includes(p));
  return [...shown, ...rest].map((p) => p.id);
}

/** A round's report as a headline and the problems worth a line each. */
export function roundSummary(report) {
  const added = report.added ?? 0;
  const headline =
    added === 0
      ? `Round ${report.round}: nothing new. ${report.found ?? 0} found, all already in this list.`
      : `Round ${report.round}: ${added} new paper${added === 1 ? '' : 's'} to screen, ` +
        `from ${report.found ?? 0} found.`;
  const problems = [];
  for (const row of [...(report.sources ?? []), ...(report.strategies ?? [])]) {
    for (const error of row.errors ?? []) problems.push(`${row.label}: ${error}`);
  }
  problems.push(...(report.notes ?? []));
  return { headline, problems };
}

/** `ranking` as the round log says it. */
export function rankingLabel(method) {
  return (
    {
      feedback: 'Ranked by similarity to your relevant papers, away from the excluded ones',
      semantic: 'Ranked by similarity to the brief',
      lexical: 'Ranked by keyword overlap with the brief (no embedding model)',
      none: 'Nothing to rank',
    }[method] ?? ''
  );
}

/** A textarea's lines as a list, blank ones dropped. */
export function lines(text) {
  return String(text ?? '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

/** The brief as the form edits it, back into the shape the API takes. */
export function briefFromForm(values) {
  const year = (value) => {
    const n = Number.parseInt(value, 10);
    return Number.isFinite(n) && n >= 1800 && n <= 2100 ? n : 0;
  };
  return {
    question: String(values.question ?? '').trim(),
    purpose: String(values.purpose ?? '').trim(),
    field: String(values.field ?? '').trim(),
    include: lines(values.include),
    exclude: lines(values.exclude),
    year_from: year(values.year_from),
    year_to: year(values.year_to),
    queries: lines(values.queries),
    language: String(values.language ?? '').trim(),
  };
}

/** Whether a brief holds enough to search on. */
export function searchable(brief) {
  return Boolean(brief && ((brief.queries ?? []).length || (brief.question ?? '').trim()));
}

// ── full texts ───────────────────────────────────────────────────────

/** What each file warning means, said to the person who can fix it. */
const WARNINGS = {
  scanned: 'No text layer: probably a scan. Nothing in it can be searched; run OCR on it and upload it again.',
  thin_pages: 'Several pages have almost no text (figures, or scanned pages). Those pages cannot be searched.',
  columns: 'Some pages are set in two columns that the PDF stores line by line across the page. They were read column by column instead; check a quotation from them against the PDF.',
  title_not_found: 'The paper’s title is not on the first pages of this PDF. Check that it is the right file.',
};

export function warningText(code) {
  return WARNINGS[code] ?? code;
}

const MATCHES = {
  doi: 'matched by DOI',
  arxiv: 'matched by arXiv id',
  title: 'matched by title',
  manual: 'assigned by you',
  upload: 'added by you',
};

// How a paper made from an uploaded PDF was described (`fulltext.adopt`).
// Everything but the registry is a guess, and its title is what every
// citation of the paper prints, so the note says which guess it was.
const DESCRIBED = {
  registry: 'described from OpenAlex',
  meta: 'title taken from the PDF’s properties',
  page: 'title read from its first page',
  file: 'titled after the file name, no title found in the PDF',
};

export function describedLabel(how) {
  return DESCRIBED[how] ?? '';
}

export function matchLabel(how) {
  return MATCHES[how] ?? '';
}

/** One file's line: pages, how it was matched, how far it is indexed. */
export function fileLine(file) {
  const bits = [`${file.pages} page${file.pages === 1 ? '' : 's'}`];
  if (file.text_pages < file.pages) bits.push(`${file.text_pages} with text`);
  const how = matchLabel(file.match);
  if (how) bits.push(how);
  if (!file.chunks) bits.push('nothing to search');
  else if (file.embedded >= file.chunks) bits.push('indexed');
  else if (file.embedded) bits.push(`${file.embedded} of ${file.chunks} passages indexed`);
  else bits.push('keyword search only');
  return bits.join(' · ');
}

/**
 * The full-text view of a reading list, grouped the way the person works
 * through it: what needs them (unassigned files), what is still missing (a
 * relevant paper with no PDF), what is ready, and what is filed but will never
 * be searched because its paper is not marked relevant.
 */
export function fulltextOverview(papers, files) {
  const byPaper = new Map();
  for (const file of files ?? []) {
    if (file.paper_id != null) byPaper.set(file.paper_id, file);
  }
  const relevant = papers.filter((p) => p.decision === 'relevant');
  const ready = relevant.filter((p) => byPaper.has(p.id)).map((paper) => ({ paper, file: byPaper.get(paper.id) }));
  const missing = relevant.filter((p) => !byPaper.has(p.id));
  const unassigned = (files ?? []).filter((f) => f.paper_id == null);
  const idle = papers
    .filter((p) => p.decision !== 'relevant' && byPaper.has(p.id))
    .map((paper) => ({ paper, file: byPaper.get(paper.id) }));
  const unindexed = (files ?? []).reduce((n, f) => n + Math.max(0, (f.chunks ?? 0) - (f.embedded ?? 0)), 0);
  return { relevant: relevant.length, ready, missing, unassigned, idle, unindexed, byPaper };
}

/** Papers to offer when assigning a file by hand: its candidates first, then
 *  the rest of the relevant and maybe papers, never the excluded. */
export function assignChoices(papers, candidates = []) {
  const wanted = papers.filter((p) => p.decision === 'relevant' || p.decision === 'maybe');
  const first = candidates.map((id) => wanted.find((p) => p.id === id)).filter(Boolean);
  return [...first, ...wanted.filter((p) => !candidates.includes(p.id))];
}
