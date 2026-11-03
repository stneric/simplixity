// Markdown → DOM. Vendored rather than pulled from a CDN, and deliberately not
// markdown-it.
//
// The security property comes from the shape of the code, not from a sanitiser:
// this builds nodes with createElement and textContent and never assigns
// innerHTML, so model output cannot become markup no matter what it contains.
// A parser that produces an HTML string always needs a sanitiser after it; this
// one has nothing to sanitise.
//
// It covers what models actually emit: headings, lists, tables, code, quotes,
// emphasis, links, inline and display math, plus [1]-style citations, which
// are the point of the app.

import { citeLink, outbound, safeUrl } from './links.js';
import { renderMath } from './tex.js';

const FENCE = /^\s*(```|~~~)\s*([\w+-]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s*>\s?(.*)$/;
const BULLET = /^(\s*)([-*+])\s+(.*)$/;
const ORDERED = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

/** Render `text` into `container`, replacing whatever was there. */
export function renderMarkdown(container, text, { sources = [] } = {}) {
  setLanguage(text);
  container.replaceChildren(...parseBlocks(String(text ?? '').split('\n'), sources));
}

// ── block level ──────────────────────────────────────────────────────

function parseBlocks(lines, sources) {
  const nodes = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fence = line.match(FENCE);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].match(FENCE)) body.push(lines[i++]);
      i += 1; // closing fence, or the end of a stream mid-block
      nodes.push(codeBlock(body.join('\n'), fence[2]));
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      const node = document.createElement(`h${heading[1].length}`);
      node.append(...parseInline(heading[2], sources));
      nodes.push(node);
      i += 1;
      continue;
    }

    if (RULE.test(line)) {
      nodes.push(document.createElement('hr'));
      i += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE.test(lines[i])) body.push(lines[i++].match(QUOTE)[1]);
      const quote = document.createElement('blockquote');
      quote.append(...parseBlocks(body, sources));
      nodes.push(quote);
      continue;
    }

    // Both display forms, because models use both and which one you get is
    // not the reader's business: `$$…$$` is the markdown convention, `\[…\]`
    // is LaTeX's own and what several models emit by default. Without this the
    // formula arrived as the literal characters `\[ … \]`.
    const opener = line.trim().startsWith('$$') ? '$$' : line.trim().startsWith('\\[') ? '\\[' : '';
    if (opener) {
      const closer = opener === '$$' ? '$$' : '\\]';
      let rest = line.trim().slice(opener.length);
      i += 1;
      const body = [];
      // The closing delimiter may share the opening line, sit on a later line,
      // or, mid-stream, not exist yet at all.
      for (;;) {
        const end = rest.indexOf(closer);
        if (end !== -1) {
          body.push(rest.slice(0, end));
          break;
        }
        body.push(rest);
        if (i >= lines.length) break;
        rest = lines[i++];
      }
      nodes.push(renderMath(body.join('\n').trim(), { display: true }));
      continue;
    }

    if (isTable(lines, i)) {
      const body = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) body.push(lines[i++]);
      nodes.push(table(body, sources));
      continue;
    }

    if (BULLET.test(line) || ORDERED.test(line)) {
      const items = [];
      while (i < lines.length) {
        if (isItem(lines, i, items)) {
          items.push(lines[i++]);
          continue;
        }
        // A blank line between two items is a *loose* list, not the end of one.
        // Models emit them constantly (often every item a paragraph apart),
        // and treating the blank as a terminator
        // started a fresh `<ol>` per item, so an ordered list rendered as "1."
        // three times over. Only a blank line followed by something that is not
        // a list item ends the list.
        const next = nextContent(lines, i);
        if (!lines[i].trim() && next !== -1 && isItem(lines, next, items)) {
          i = next;
          continue;
        }
        break;
      }
      nodes.push(list(items, sources));
      continue;
    }

    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) paragraph.push(lines[i++]);
    if (paragraph.length) {
      const node = document.createElement('p');
      node.append(...parseInline(paragraph.join('\n'), sources));
      nodes.push(node);
    } else {
      i += 1; // never spin: a line that starts a block was handled above
    }
  }

  return nodes;
}

const startsBlock = (lines, i) =>
  HEADING.test(lines[i]) ||
  RULE.test(lines[i]) ||
  QUOTE.test(lines[i]) ||
  FENCE.test(lines[i]) ||
  BULLET.test(lines[i]) ||
  ORDERED.test(lines[i]) ||
  isTable(lines, i);

const isTable = (lines, i) =>
  lines[i].includes('|') && i + 1 < lines.length && TABLE_DIVIDER.test(lines[i + 1]);

// An indented, non-empty line directly under a list item belongs to that item.
const isContinuation = (lines, i, items) =>
  items.length > 0 && /^\s{2,}\S/.test(lines[i]);

const isItem = (lines, i, items) =>
  BULLET.test(lines[i]) || ORDERED.test(lines[i]) || isContinuation(lines, i, items);

/** Index of the next non-blank line at or after `i`, or -1 if there is none. */
const nextContent = (lines, i) => {
  let j = i;
  while (j < lines.length && !lines[j].trim()) j += 1;
  return j < lines.length ? j : -1;
};

function codeBlock(code, language) {
  const wrap = document.createElement('div');
  wrap.className = 'code-wrap';

  const pre = document.createElement('pre');
  const node = document.createElement('code');
  if (language) node.dataset.language = language;
  node.textContent = code;
  pre.append(node);

  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'code-copy';
  copy.textContent = 'Copy';
  copy.addEventListener('click', async () => {
    // Read from the DOM, not the closure: a streamed answer keeps this button
    // across renders while the code above it grows (`morph` in blocks.js).
    await navigator.clipboard?.writeText(node.textContent);
    copy.textContent = 'Copied';
    setTimeout(() => (copy.textContent = 'Copy'), 1200);
  });

  wrap.append(pre, copy);
  return wrap;
}

function list(lines, sources) {
  // Flatten to {depth, ordered, text}, then rebuild as a tree. Doing it in one
  // pass with a stack is where nested-list renderers usually go wrong.
  const items = [];
  for (const line of lines) {
    const match = line.match(BULLET) || line.match(ORDERED);
    if (!match) {
      // A continuation line joins the previous item.
      if (items.length) items[items.length - 1].text += `\n${line.trim()}`;
      continue;
    }
    items.push({
      depth: Math.floor(match[1].replace(/\t/g, '  ').length / 2),
      ordered: ORDERED.test(line),
      text: match[3],
    });
  }

  const build = (start, depth) => {
    const root = document.createElement(items[start].ordered ? 'ol' : 'ul');
    let i = start;
    while (i < items.length && items[i].depth >= depth) {
      if (items[i].depth > depth) {
        const [child, next] = build(i, items[i].depth);
        (root.lastElementChild ?? root).append(child);
        i = next;
        continue;
      }
      const item = document.createElement('li');
      item.append(...parseInline(items[i].text, sources));
      root.append(item);
      i += 1;
    }
    return [root, i];
  };

  return items.length ? build(0, items[0].depth)[0] : document.createElement('ul');
}

const cells = (line) =>
  line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());

// A cell that is a quantity: an optional sign or currency, digits with any
// thousands and decimal separators, an optional unit or percent. Deliberately
// loose about which separator is which: "1.234,5 €" and "1,234.5%" are both
// numbers, and this only decides an alignment.
const NUMERIC_CELL = /^[+\-−(]?\s*[$€£¥]?\s*\d[\d\s.,'’]*\s*(%|[a-zA-Z°/²³]{1,12})?\s*\)?$/u;

// A column of numbers is unreadable ragged-left, and models forget the `---:`
// syntax about half the time, so the divider row is honoured when it says
// something, and inferred from the data when it does not.
function alignments(dividerLine, bodyRows, columns) {
  const declared = cells(dividerLine).map((cell) => {
    const left = cell.startsWith(':');
    const right = cell.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    if (left) return 'left';
    return '';
  });

  return Array.from({ length: columns }, (_, index) => {
    if (declared[index]) return declared[index];
    const column = bodyRows.map((row) => row[index]).filter((cell) => cell != null && cell !== '');
    // Inferred only when the column is entirely numeric: one stray "n/a" is
    // fine, a column of labels with two numbers in it is not a numeric column.
    const numeric = column.filter((cell) => NUMERIC_CELL.test(cell.replace(/\[\d+\]/g, '').trim()));
    return column.length && numeric.length >= Math.max(2, column.length - 1) ? 'right' : '';
  });
}

function table(lines, sources) {
  const header = cells(lines[0]);
  const bodyRows = lines.slice(2).map(cells);
  const columns = header.length;
  const align = alignments(lines[1] ?? '', bodyRows, columns);

  const node = document.createElement('table');
  // Two columns with a header the model left blank is a key/value block (the
  // shape a figure's context comes in), and it reads better without the grid.
  if (columns === 2 && header.every((cell) => !cell)) node.className = 'kv';

  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  header.forEach((cell, index) => {
    const th = document.createElement('th');
    if (align[index]) th.dataset.align = align[index];
    th.append(...parseInline(cell, sources));
    headRow.append(th);
  });
  head.append(headRow);

  const body = document.createElement('tbody');
  for (const row of bodyRows) {
    const tr = document.createElement('tr');
    row.forEach((cell, index) => {
      const td = document.createElement('td');
      if (align[index]) td.dataset.align = align[index];
      td.append(...parseInline(cell, sources));
      tr.append(td);
    });
    body.append(tr);
  }

  node.append(head, body);
  const scroller = document.createElement('div');
  scroller.className = 'table-wrap';
  scroller.append(node);
  return scroller;
}

// ── inline level ─────────────────────────────────────────────────────

// Ordered by precedence. Code first, so `**` inside a code span stays literal.
// A passage citation's parts, see the rule below.
const OPEN = '"“„«»';
const CLOSE = '"”“»«';
// A passage ends only at a closing mark followed by the bracket's end or the
// next passage, so it may hold quotation marks and brackets of its own
// (`verify._PASSAGE_CITATION` explains why; the two must agree). The next
// passage is set off by a comma, a semicolon or a space: a passage ending in a
// quotation of its own, `… “walls”"]`, has a closing mark right before its own
// closing one, and read as "end, and the next passage starts" it broke the
// citation.
const END = `[${CLOSE}](?:\\s*\\]|\\s*[,;]\\s*[${OPEN}]|\\s+[${OPEN}])`;
const ONE = `[${OPEN}](?:(?!${END})[^\\n]){3,600}[${CLOSE}]`;
// `[4: "…"]` anchors a statement at the words that carry it, and only the
// number is shown. `[4 quote: "…"]` was a direct quotation; writers are no
// longer asked for one, and an answer saved with one shows its words as plain
// quoted text. Group 2 says which.
const PASSAGE_CITATION = new RegExp(`\\[(\\d{1,3})(\\s*[Qq]uote)?:\\s*((?:${ONE}\\s*[,;]?\\s*)+)\\]`);
// Over a citation's inside only, cut before its `]`: the last passage ends the text.
const PASSAGE = new RegExp(
  `[${OPEN}]((?:(?![${CLOSE}](?:\\s*$|\\s*[,;]\\s*[${OPEN}]|\\s+[${OPEN}]))[^\\n]){3,600})[${CLOSE}]`,
  'g',
);

/** The passages in a citation's bracket, whitespace folded. */
function passagesOf(text) {
  return [...text.matchAll(PASSAGE)].map((m) => m[1].replace(/\s+/g, ' ').trim());
}

const INLINE = [
  { pattern: /`([^`]+)`/, render: (m) => tag('code', m[1]) },
  // Math before emphasis: inside $…$, ^ and _ are TeX, not markdown. The gap
  // guards keep "$5 and $10" literal: a currency pair is not a formula.
  { pattern: /\$([^\s$](?:[^$]*[^\s$])?)\$/, render: (m) => renderMath(m[1]) },
  // LaTeX's own inline delimiters. No gap guards needed: unlike `$`, nothing
  // writes `\(` for any other reason, so anything between them is a formula.
  { pattern: /\\\(([\s\S]+?)\\\)/, render: (m) => renderMath(m[1].trim()) },
  { pattern: /\*\*([^*]+)\*\*/, render: (m, s) => wrap('strong', m[1], s) },
  { pattern: /__([^_]+)__/, render: (m, s) => wrap('strong', m[1], s) },
  { pattern: /(?<![*\w])\*([^*\n]+)\*(?!\*)/, render: (m, s) => wrap('em', m[1], s) },
  { pattern: /(?<![_\w])_([^_\n]+)_(?!\w)/, render: (m, s) => wrap('em', m[1], s) },
  { pattern: /~~([^~]+)~~/, render: (m, s) => wrap('del', m[1], s) },
  { pattern: /!\[([^\]]*)\]\(([^)\s]+)\)/, render: (m) => image(m[2], m[1]) },
  { pattern: /\[([^\]]+)\]\(([^)\s]+)\)/, render: (m, s) => link(m[2], m[1], s) },
  // `[4: "exact words"]`: a citation of a passage, in a literature project
  // (app/agent/prompts.py, LIBRARY_RULES). Before the bare number, which would
  // otherwise never match it but leave the words in the prose.
  // Several passages of one page may share the bracket, and a passage may hold
  // a bracket of its own ("… every month [14]"), so only a quotation mark ends
  // one; app/agent/verify.py parses the same shape.
  { pattern: PASSAGE_CITATION, render: (m, s) => citation(Number(m[1]), s, passagesOf(m[3]), Boolean(m[2])), cites: true },
  { pattern: /\[(\d{1,3})\]/, render: (m, s) => citation(Number(m[1]), s), cites: true },
  // No rule for words in quotation marks. There was one, highlighting them as
  // a direct quotation, and it could not tell a quotation from a German „term“
  // followed by a passage citation: it paired the „ with the citation's own "
  // and highlighted the sentence between, citation and all.
  { pattern: /(?<![\w@.])(https?:\/\/[^\s<>()]+[^\s<>().,;:!?])/, render: (m) => autolink(m[1]) },
];

function parseInline(text, sources = []) {
  let earliest = null;

  for (const rule of INLINE) {
    const match = text.match(rule.pattern);
    if (match && (earliest === null || match.index < earliest.match.index)) {
      earliest = { rule, match };
    }
  }

  if (!earliest) return [document.createTextNode(text)];

  const { rule, match } = earliest;
  let rest = text.slice(match.index + match[0].length);
  let node;
  if (rule.cites) {
    // The same page cited twice in a row, `[1: "…"][1: "…"]`, is one
    // citation: two identical circles side by side read as two sources. The
    // passages of both travel with the one link.
    const number = Number(match[1]);
    const quotes = match[3] ? passagesOf(match[3]) : [];
    let direct = Boolean(match[2]);
    for (;;) {
      const next = adjacentCitation(rest);
      if (!next || Number(next.match[1]) !== number) break;
      if (next.match[3]) quotes.push(...passagesOf(next.match[3]));
      direct ||= Boolean(next.match[2]);
      rest = rest.slice(next.length);
    }
    node = citation(number, sources, quotes, direct);
  } else {
    node = rule.render(match, sources);
  }
  // A citation number with no matching source stays literal text. The
  // alternative is a dead link implying evidence that does not exist. It is
  // emitted as a text node rather than re-parsed, or the same rule would match
  // it again forever.
  node ??= document.createTextNode(match[0]);
  // Writers put two passage citations side by side, `[1: "…"][3: "…"]`. As
  // numbers that read fine; written out it ran "[…, p. 1]we scale" together.
  const gap = node.className === 'reference' && /^[^\s.,;:!?)]/.test(rest);

  return [
    ...(match.index ? parseInline(text.slice(0, match.index), sources) : []),
    node,
    ...(gap ? [document.createTextNode(' ')] : []),
    ...parseInline(rest, sources),
  ];
}

/** A citation starting `text`, after nothing but spaces, with its length. */
function adjacentCitation(text) {
  const lead = /^\s*/.exec(text)[0].length;
  const tail = text.slice(lead);
  const match = tail.match(PASSAGE_CITATION) ?? tail.match(/\[(\d{1,3})\]/);
  if (!match || match.index !== 0) return null;
  return { match, length: lead + match[0].length };
}

function tag(name, text) {
  const node = document.createElement(name);
  node.textContent = text;
  return node;
}

function wrap(name, text, sources) {
  const node = document.createElement(name);
  node.append(...parseInline(text, sources));
  return node;
}

/** Only http(s) survives: `javascript:` and `data:` never reach an href. */
function link(url, text, sources = []) {
  if (!safeUrl(url)) return tag('span', text);
  const node = outbound(url);
  node.append(...parseInline(text, sources));
  return node;
}

/** A bare URL in the prose. Its label is the URL, so it must NOT be re-parsed:
 *  the autolink rule would match its own output and recurse until the stack
 *  gives out. */
function autolink(url) {
  if (!safeUrl(url)) return tag('span', url);
  const node = outbound(url);
  node.textContent = url;
  return node;
}

function image(url, alt) {
  const safe = safeUrl(url);
  if (!safe) return tag('span', alt);
  const node = document.createElement('img');
  node.src = safe;
  node.alt = alt;
  node.loading = 'lazy';
  return node;
}

function citation(number, sources, quotes = [], direct = false) {
  const list = sources ?? [];
  const source = list.find((s) => s.index === number) ?? list[number - 1];
  if (source?.byline) return reference(number, list, source, quotes, direct);
  return citeLink(number, sources, 'cite', quotes);
}

// A page of the person's own documents is cited as its paper and page, `1, S. 33`,
// in the small grey pill every citation in the app wears (`.cite`), with the
// reference and the anchored words in its hover card. The writer numbers
// pages, one source per PDF page, so its numbers were nonsense to a reader:
// two pages of one paper were "4" and "7", and the same page twice was "1 1".
// The paper's number comes from `paperNumbers`, which the source list uses too. Written out in the prose ("vgl. Vaswani et al. 2017, S. 5"
// in a box after every sentence) it was the loudest thing on the page. The
// words a citation is anchored at still travel with the link, highlighted when
// the page opens and checked word for word, but they are not a quotation and
// never stand in the text. Copy still writes the full reference
// (`citedText`), which is what a thesis needs. Only sources that carry a
// `byline` are cited so: the server sets it for library pages alone.
//
// "S." or "p.", "vgl." or "cf.": the answer's language decides, not the
// browser's. A German answer read in an English browser printed "cf. … p. 5"
// into a German sentence. Set per render (`renderMarkdown`, `citedText`).
// Text too short to tell is treated as English, like the rest of the interface.
let GERMAN = false;
let MARKS = ['"', '"'];

/** Whether a text is German, by its commonest short words. */
export function isGerman(text) {
  const words = String(text ?? '').toLowerCase().match(/\p{L}+/gu) ?? [];
  if (words.length < 8) return false;
  const de = new Set(['und', 'der', 'die', 'das', 'nicht', 'ist', 'mit', 'von', 'sie', 'den', 'ein', 'eine', 'auf', 'für', 'auch', 'sich', 'dass', 'wird', 'werden', 'zu']);
  const en = new Set(['the', 'and', 'of', 'to', 'is', 'that', 'with', 'for', 'are', 'this', 'it', 'as', 'by', 'on', 'they', 'an', 'be', 'not', 'from', 'their']);
  const score = words.reduce((n, w) => n + (de.has(w) ? 1 : en.has(w) ? -1 : 0), 0);
  return score > 0;
}

function setLanguage(text) {
  GERMAN = isGerman(text);
  MARKS = GERMAN ? ['„', '"'] : ['"', '"'];
}

// A source's title ends in where on its document it is; without that, the
// pages of one paper share it.
const WHERE = / \((?:PDF p\. \d+|part \d+|abstract only|abstract)\)$/;

/**
 * Paper numbers for the sources of a source-bound answer: source index →
 * the number of the document it is a page of.
 *
 * The run numbers sources one per PDF page. A reader counts papers, so pages
 * of one paper (same title before its page, same byline) share a number, in
 * the order the run first listed one of their pages. The source list
 * (blocks.js) shows the same numbers, so "1" in the prose is "1" there.
 * Sources without a byline (the web) keep their own index.
 */
export function paperNumbers(sources = []) {
  const byPaper = new Map();
  const numbers = new Map();
  for (const source of [...sources].sort((a, b) => (a.index ?? 0) - (b.index ?? 0))) {
    if (!source.byline) continue;
    const key = `${(source.title ?? '').replace(WHERE, '')}|${source.byline}`;
    if (!byPaper.has(key)) byPaper.set(key, byPaper.size + 1);
    numbers.set(source.index, byPaper.get(key));
  }
  return numbers;
}

/** The page of a library source, as its unit and number, or "". */
function pageOf(source) {
  const page = /^\/library\/[^/]+\/(\d+)/.exec(source.url ?? '')?.[1];
  if (!page) return '';
  const parts = / \(part \d+\)$/.test(source.title ?? '');
  return `${parts ? (GERMAN ? 'Teil' : 'part') : GERMAN ? 'S.' : 'p.'} ${page}`;
}

/** "1, S. 33": the paper's number and the page, what a library citation shows. */
export function citeLabel(source, sources) {
  const number = paperNumbers(sources).get(source.index) ?? source.index;
  const page = pageOf(source);
  return page ? `${number}, ${page}` : String(number);
}

/** "Vaswani et al. 2017, S. 5", with "vgl." in front unless the citation is a
 *  direct quotation. The page from the link, which is the page the reader
 *  opens, and "part" for a file that has parts rather than pages. */
export function referenceLabel(source, direct = false) {
  const page = /^\/library\/[^/]+\/(\d+)/.exec(source.url ?? '')?.[1];
  const parts = / \(part \d+\)$/.test(source.title ?? '');
  const unit = parts ? (GERMAN ? 'Teil' : 'part') : GERMAN ? 'S.' : 'p.';
  const where = page ? `${source.byline}, ${unit} ${page}` : source.byline;
  return direct ? where : `${GERMAN ? 'vgl.' : 'cf.'} ${where}`;
}

/** The answer as text to paste into a thesis: each citation of a library page
 *  written out as the reader sees it, in parentheses, every other citation
 *  left as it was. The Copy button used to hand over `[4: "…"]`, the writer's
 *  markup. */
export function citedText(markdown, sources = []) {
  setLanguage(markdown);
  const find = (number) => sources.find((s) => s.index === number) ?? sources[number - 1];
  const written = (number, quotes, direct, raw) => {
    const source = find(number);
    if (!source?.byline) return raw;
    const quoted = direct ? quotes.map((text) => `${MARKS[0]}${text}${MARKS[1]}`).join(' … ') : '';
    return `${quoted ? `${quoted} ` : ''}(${referenceLabel(source, direct)})`;
  };
  return markdown
    .replace(new RegExp(PASSAGE_CITATION.source, 'g'), (raw, n, direct, inner) =>
      written(Number(n), passagesOf(inner), Boolean(direct), raw),
    )
    .replace(/\[(\d{1,3})\]/g, (raw, n) => written(Number(n), [], false, raw));
}

function reference(number, sources, source, quotes, direct) {
  const node = document.createElement('span');
  node.className = 'reference';
  if (direct && quotes.length) {
    // An answer saved when writers still quoted directly: the words are part
    // of its sentence, so they stay, as plain quoted text.
    node.append(document.createTextNode(`${quotes.map((q) => `${MARKS[0]}${q}${MARKS[1]}`).join(' … ')} `));
  }
  const link = citeLink(number, sources, 'cite', quotes, citeLabel(source, sources));
  // Named in full for a screen reader; on screen the hover card names it.
  link?.setAttribute('aria-label', referenceLabel(source, direct && quotes.length > 0));
  node.append(link ?? document.createTextNode(`[${number}]`));
  return node;
}
