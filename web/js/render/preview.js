// Hover card for a source: favicon and title, then the opening of the text.
//
// Shared by inline citations and by the cards in the source list, because it is
// the same question in both places: is this worth opening? Following ten links
// to find out is not reading an answer.
//
// One element on <body>, refilled and moved. The prose is re-rendered on every
// streamed patch and the source list on every new source, so nothing stateful
// may hang off a citation's own (disposable) subtree.

import { el } from '../shared/dom.js';

const GAP = 14;
const EDGE = 8;

let card = null;

/** Attach the card to a node. Returns the node, so it composes into a builder. */
export function attachPreview(node, source) {
  if (!source) return node;
  node.addEventListener('mouseenter', (event) => show(source, event));
  node.addEventListener('mousemove', move);
  node.addEventListener('mouseleave', hide);
  return node;
}

/**
 * Run `handler` when an image did not actually arrive.
 *
 * `error` alone is not enough here, and for a while nothing in this app that
 * depended on it worked. Both image proxies answer *every* failure with HTTP
 * 200 and a 1×1 transparent GIF, so that a widget which has
 * already been laid out cannot reflow because some host was slow. The cost is
 * that the browser considers the load a success, `error` never fires, and every
 * fallback hanging off it is dead code: the missing-image note, the flag that
 * removes itself, the letter that stands in for a favicon. None of them ever
 * appeared; the reader just saw an empty box.
 *
 * So the load handler asks the only question that distinguishes the two: an
 * image one pixel wide is the placeholder, not a picture.
 */
export function onBrokenImage(img, handler) {
  img.addEventListener('error', handler);
  img.addEventListener('load', () => {
    if (img.naturalWidth <= 1 && img.naturalHeight <= 1) handler();
  });
  return img;
}

function element() {
  if (card) return card;
  card = document.createElement('div');
  card.className = 'cite-preview';
  card.hidden = true;
  card.setAttribute('role', 'tooltip');
  document.body.append(card);
  // If the thread is replaced under an open card, its anchor's mouseleave never
  // fires. Any click or scroll is reason enough to let go.
  window.addEventListener('scroll', hide, true);
  document.addEventListener('click', hide);
  return card;
}

function show(source, event) {
  const node = element();
  const host = hostOf(source);

  const children = [head(source, host)];
  const meta = metaLine(source, host);
  if (meta) children.push(meta);
  // A passage citation shows the words it points to, not the page's opening.
  if (source.quotes?.length) {
    for (const quote of source.quotes) children.push(el('div', 'cite-preview-quote', `“${quote}”`));
  }
  else if (source.snippet) children.push(el('div', 'cite-preview-snippet', source.snippet));
  const foot = footLine(source);
  if (foot) children.push(foot);
  node.replaceChildren(...children);

  node.hidden = false;
  move(event);
}

function head(source, host) {
  const row = el('div', 'cite-preview-head');
  if (host) row.append(favicon(host));
  row.append(el('span', 'cite-preview-title', source.title || host || source.url));
  return row;
}

function favicon(host) {
  const img = document.createElement('img');
  img.className = 'cite-preview-favicon';
  img.alt = '';
  // Proxied through this origin rather than fetched from the source's own: the
  // page then talks only to this server, and the sites in a list the reader has
  // not opened do not learn that it was assembled.
  img.src = `/api/favicon/${encodeURIComponent(host)}`;
  onBrokenImage(img, () => img.replaceWith(letter(host)));
  return img;
}

function letter(host) {
  return el('span', 'cite-preview-favicon fallback', host[0].toUpperCase());
}

/** Publisher, what kind of source it is, and when: the provenance the ranking
 *  used, in the place where the reader is deciding whether to believe it. */
function metaLine(source, host) {
  if (!host && !source.tier && !source.byline) return null;
  const row = el('div', 'cite-preview-domain');
  // A page of the person's papers is cited as a bare `(4)`: the card is where
  // it says whose paper that is.
  if (source.byline) row.append(el('span', null, source.byline));
  if (host) row.append(el('span', null, host));
  if (source.tier && source.tier !== 'web') {
    row.append(el('span', `tier tier-${source.tier}`, source.tier_label ?? source.tier));
  }
  if (source.published) row.append(el('span', null, String(source.published).slice(0, 10)));
  return row;
}

/** Whether this is the page or two lines an engine wrote about it (the
 *  difference between evidence and a hint), and which engine found it. */
function footLine(source) {
  const bits = [
    source.read ? 'Read in full' : 'Search snippet only',
    source.engines?.length ? `found by ${source.engines.join(', ')}` : '',
  ].filter(Boolean);
  return bits.length ? el('div', 'cite-preview-foot', bits.join(' · ')) : null;
}

function move(event) {
  const node = element();
  if (node.hidden) return;
  const { width, height } = node.getBoundingClientRect();
  let x = event.clientX + GAP;
  let y = event.clientY + GAP;
  // Flip to the other side of the cursor rather than clipping at the edge.
  if (x + width > innerWidth - EDGE) x = event.clientX - width - GAP;
  if (y + height > innerHeight - EDGE) y = event.clientY - height - GAP;
  node.style.left = `${Math.max(EDGE, x)}px`;
  node.style.top = `${Math.max(EDGE, y)}px`;
}

function hide() {
  if (card) card.hidden = true;
}

function hostOf(source) {
  if (source.domain) return source.domain;
  try {
    return new URL(source.url).hostname.replace(/^www\./, '');
  } catch {
    return ''; // an unparseable URL simply gets no favicon line
  }
}

