// The reader: one of the person's PDFs, opened on the page a citation names,
// with the passages the answer rests on highlighted where they stand.
//
// A citation in a literature project is a page, `[4] … (PDF p. 5)`, and the
// browser's own PDF handling made that worthless: depending on its settings it
// downloaded the file, and even when it showed it, the reader was left to find
// the sentence on a page of two columns by eye. So the page is rendered here,
// with pdf.js (vendored under web/vendor/pdfjs, no CDN), and its text layer is
// searched for two kinds of passage the citation carries in its fragment
// (reader-core.js):
//
// * **evidence**, strong: the exact words the citation check found carrying a
//   claim, verified against the page text on the server before they got here;
// * **passages**, light: what the answer was written from, the context.
//
// Whatever cannot be located on the rendered page is still listed in the side
// panel, with the words, and says so. A highlight that silently fails looks
// exactly like a page that does not contain the passage.
//
// A Word, Markdown or text file attached to a chat has no pages to draw. It is
// shown as the text it was read as, part by part (app/tools/documents.py),
// which is exactly the text its citations were checked against, one span per
// word so a cited passage is marked to the word.

import * as pdfjs from '../../vendor/pdfjs/pdf.min.mjs';
import { hydrate } from '../shared/icons.js';
import { ensureProfile } from '../shared/profile.js';
import { locate, parseHash } from './reader-core.js';
import { el } from '../shared/dom.js';

hydrate();
pdfjs.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';

const $ = (id) => document.getElementById(id);
// Wide enough to read a two-column paper at a glance, never wider: past this a
// line of text is longer than the eye can follow back.
const MAX_WIDTH = 920;
const [, , fileId, pageParam] = location.pathname.split('/');
const target = Math.max(1, Number.parseInt(pageParam, 10) || 1);
const wanted = parseHash(location.hash);
const pagesRoot = $('reader-pages');
const side = $('reader-side');
const pages = []; // { node, number, rendered, layer }
let doc = null;
let current = target;

// ── side panel ───────────────────────────────────────────────────────

const entries = []; // { text, kind, node, found }, kind: cited | evidence | passage

function renderSide() {
  side.replaceChildren();
  const groups = [
    ['cited', 'Cited passage', 'The words this citation points to, checked to be on this page.'],
    ['evidence', wanted.cited.length ? 'Also cited on this page' : 'Supporting the answer', 'The exact words carrying the answer’s other claims here.'],
    ['passage', 'Passages the answer used', 'What the answer was written from on this page.'],
  ];
  if (!entries.length) {
    side.append(
      el('p', 'reader-side-head', `Page ${target}`),
      el('p', 'hint', 'This citation carries no passages to mark: the answer that cited it was written before the reader existed. The whole page is what it cites.'),
    );
    return;
  }
  for (const [kind, title, hint] of groups) {
    const list = entries.filter((e) => e.kind === kind);
    if (!list.length) continue;
    side.append(el('p', 'reader-side-head', title), el('p', 'hint', hint));
    for (const entry of list) {
      const item = el('button', `reader-passage ${kind}${entry.found === false ? ' missing' : ''}`);
      item.type = 'button';
      item.append(el('span', 'reader-passage-text', entry.text));
      if (entry.found === false) {
        item.append(el('span', 'reader-passage-note', 'Not located on the rendered page: compare by eye.'));
      }
      item.addEventListener('click', () => {
        if (entry.kind === 'passage') {
          for (const span of pagesRoot.querySelectorAll('.hl-passage.picked')) span.classList.remove('hl-passage', 'picked');
          entry.spans?.forEach((span) => span.classList.add('hl-passage', 'picked'));
        }
        entry.spans?.[0]?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      });
      entry.node = item;
      side.append(item);
    }
  }
}

// ── pages ────────────────────────────────────────────────────────────

function scaleFor(page) {
  const base = page.getViewport({ scale: 1 });
  const room = Math.min(MAX_WIDTH, pagesRoot.clientWidth - 32);
  return Math.max(0.5, room / base.width);
}

async function render(entry) {
  if (entry.rendered) return entry.rendered;
  entry.rendered = (async () => {
    const page = await doc.getPage(entry.number);
    const viewport = page.getViewport({ scale: scaleFor(page) });
    const { node } = entry;
    node.style.width = `${viewport.width}px`;
    node.style.height = `${viewport.height}px`;
    node.style.setProperty('--scale-factor', String(viewport.scale));

    const ratio = window.devicePixelRatio || 1;
    const canvas = el('canvas', 'reader-canvas');
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    const layer = el('div', 'textLayer');
    node.replaceChildren(canvas, layer);

    await page.render({
      canvas,
      canvasContext: canvas.getContext('2d'),
      viewport,
      transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
    }).promise;
    const text = new pdfjs.TextLayer({ textContentSource: page.streamTextContent(), container: layer, viewport });
    await text.render();
    entry.layer = text;
    if (entry.number === target) mark(text);
  })();
  return entry.rendered;
}

/** Highlight the wanted passages on the cited page and report which landed.
 *
 *  The checked evidence is a sentence; the passages the answer used are whole
 *  paragraphs, and a page of them in yellow marks nothing. So when evidence is
 *  found, only it is marked, and a passage is marked when it is picked in the
 *  side panel. Without evidence, an answer written before the check could
 *  supply it, the passages are what there is, and they are marked lightly. */
function mark(text) {
  const spans = text.textDivs ?? [];
  const items = text.textContentItemsStr ?? spans.map((s) => s.textContent);
  for (const entry of entries) {
    const indices = locate(items, entry.text);
    entry.found = indices.length > 0;
    entry.spans = indices.map((i) => spans[i]).filter(Boolean);
  }
  const precise = entries.some((e) => e.kind !== 'passage' && e.found);
  const cited = entries.some((e) => e.kind === 'cited' && e.found);
  for (const entry of entries) {
    // The passage this citation names is the one to look at; the page's other
    // cited words stay visible, quieter, so the eye lands on the right one.
    const kind = {
      cited: 'hl-evidence',
      evidence: cited ? 'hl-other' : 'hl-evidence',
      passage: precise ? '' : 'hl-passage',
    }[entry.kind];
    // A text item holds a whole line, so the quieter marks never land on an
    // item the cited passage already holds: it would be painted over.
    if (kind) {
      entry.spans
        .filter((span) => kind === 'hl-evidence' || !span.classList.contains('hl-evidence'))
        .forEach((span) => span.classList.add(kind));
    }
  }
  renderSide();
  const first =
    entries.find((e) => e.kind === 'cited' && e.found) ??
    entries.find((e) => e.kind === 'evidence' && e.found) ??
    entries.find((e) => e.found);
  first?.spans[0]?.scrollIntoView({ block: 'center' });
}

// What one numbered piece of the file is called: a page of a PDF, a part of
// anything else.
let unit = 'Page';

function setCurrent(number) {
  current = Math.min(Math.max(1, number), pages.length || 1);
  $('reader-pageno').textContent = `${unit} ${current} of ${pages.length}`;
  $('reader-prev').disabled = current <= 1;
  $('reader-next').disabled = current >= pages.length;
}

function go(number) {
  const entry = pages[number - 1];
  if (!entry) return;
  render(entry);
  entry.node.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

async function open() {
  const info = await fetch(`/api/literature/files/${fileId}`).then((res) => (res.ok ? res.json() : null));
  if (!info) {
    $('reader-title').textContent = 'This file does not exist any more, or belongs to another profile.';
    return;
  }
  const pdf = (info.file.kind ?? 'pdf') === 'pdf';
  unit = pdf ? 'Page' : 'Part';
  const data = info.paper?.data ?? {};
  const title = data.title || info.file.name;
  document.title = `${title} · ${pdf ? 'p.' : 'part'} ${target}`;
  $('reader-title').textContent = title;
  const authors = (data.authors ?? []).slice(0, 3).join(', ') + ((data.authors ?? []).length > 3 ? ' et al.' : '');
  $('reader-meta').textContent = [authors, data.year, data.venue, pdf ? `PDF p. ${target}` : `Part ${target}`]
    .filter(Boolean)
    .join(' · ');
  const download = $('reader-download');
  download.href = `/api/literature/files/${fileId}/pdf`;
  download.setAttribute('download', info.file.name);
  download.querySelector('span').textContent = { pdf: 'PDF', docx: 'Word', md: 'Markdown' }[info.file.kind ?? 'pdf'] ?? 'File';

  for (const text of wanted.cited) entries.push({ text, kind: 'cited' });
  for (const text of wanted.evidence) {
    if (!wanted.cited.includes(text)) entries.push({ text, kind: 'evidence' });
  }
  for (const text of wanted.passages) entries.push({ text, kind: 'passage' });
  renderSide();

  if (!pdf) {
    await openText();
    return;
  }

  try {
    doc = await pdfjs.getDocument({ url: `/api/literature/files/${fileId}/pdf`, isEvalSupported: false }).promise;
  } catch (error) {
    pagesRoot.replaceChildren(el('p', 'profile-error', `The PDF could not be opened: ${error?.message ?? error}`));
    return;
  }

  // Placeholders sized from the first page, so the scroll position of the
  // cited page is right before anything around it has been drawn.
  const first = await doc.getPage(1);
  const size = first.getViewport({ scale: scaleFor(first) });
  for (let number = 1; number <= doc.numPages; number += 1) {
    const node = el('div', 'reader-page');
    node.dataset.page = String(number);
    node.style.width = `${size.width}px`;
    node.style.height = `${size.height}px`;
    node.append(el('span', 'reader-page-label', `Page ${number}`));
    pagesRoot.append(node);
    pages.push({ node, number, rendered: null, layer: null });
  }
  setCurrent(target);

  // Draw what comes into view, a little ahead; track which page is current.
  const draw = new IntersectionObserver(
    (seen) => seen.filter((s) => s.isIntersecting).forEach((s) => render(pages[Number(s.target.dataset.page) - 1])),
    { root: pagesRoot, rootMargin: '600px 0px' },
  );
  const track = new IntersectionObserver(
    (seen) => {
      const visible = seen.filter((s) => s.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (visible) setCurrent(Number(visible.target.dataset.page));
    },
    { root: pagesRoot, threshold: [0.25, 0.5, 0.75] },
  );
  for (const { node } of pages) {
    draw.observe(node);
    track.observe(node);
  }
  const cited = pages[target - 1] ?? pages[0];
  cited.node.scrollIntoView({ block: 'start' });
  cited.node.classList.add('cited');
  await render(cited);
}

/** A file without pages: its parts as text, every word its own span, so the
 *  same `locate` that marks a PDF's text layer marks these. */
async function openText() {
  const found = await fetch(`/api/literature/files/${fileId}/pages`).then((res) => (res.ok ? res.json() : null));
  if (!found) {
    pagesRoot.replaceChildren(el('p', 'profile-error', 'The text of this file could not be loaded.'));
    return;
  }
  found.pages.forEach((text, index) => {
    const number = index + 1;
    const node = el('div', 'reader-page reader-text');
    node.dataset.page = String(number);
    node.append(el('span', 'reader-page-label', `Part ${number}`));
    const spans = [];
    for (const line of String(text).split('\n')) {
      const paragraph = el('p');
      for (const word of line.split(/\s+/).filter(Boolean)) {
        const span = el('span', '', word);
        spans.push(span);
        paragraph.append(span, ' ');
      }
      node.append(paragraph);
    }
    pagesRoot.append(node);
    pages.push({ node, number, rendered: Promise.resolve(), layer: { textDivs: spans } });
  });
  setCurrent(target);
  const track = new IntersectionObserver(
    (seen) => {
      const visible = seen.filter((s) => s.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (visible) setCurrent(Number(visible.target.dataset.page));
    },
    { root: pagesRoot, threshold: [0.25, 0.5, 0.75] },
  );
  for (const { node } of pages) track.observe(node);
  const cited = pages[target - 1] ?? pages[0];
  if (!cited) return;
  cited.node.scrollIntoView({ block: 'start' });
  cited.node.classList.add('cited');
  mark(cited.layer);
}

$('reader-prev').addEventListener('click', () => go(current - 1));
$('reader-next').addEventListener('click', () => go(current + 1));
document.addEventListener('keydown', (event) => {
  if (event.target.closest?.('input, textarea')) return;
  if (event.key === 'ArrowLeft') go(current - 1);
  if (event.key === 'ArrowRight') go(current + 1);
});

await ensureProfile();
await open();
