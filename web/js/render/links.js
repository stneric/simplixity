// Every outbound link and citation in the app, built one way.
//
// There were six hand-written copies of "anchor, new tab, noopener", in
// `markdown.js` and `blocks.js`, and only the markdown ones checked the scheme.
// The server only ever registers http(s) sources, so the others were safe by
// what reached them rather than by what they did, which is the kind of safety
// that lasts until the first new source type. One helper, one check.

import { attachPreview } from './preview.js';
import { sourceHash } from '../reader/reader-core.js';

/** The URL if it is http(s), resolved against this page, else null. The
 *  project's own chats are linked as `/#chat=…`, which resolves to this origin
 *  and passes. */
export function safeUrl(url) {
  try {
    const parsed = new URL(url, location.origin);
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}

/** An anchor that opens in a new tab and cannot reach back into this one.
 *  An unsafe URL keeps the node and loses the link: the text still reads, the
 *  click goes nowhere. */
export function outbound(url, className) {
  const node = document.createElement('a');
  if (className) node.className = className;
  const safe = safeUrl(url);
  if (safe) {
    node.href = safe;
    node.target = '_blank';
    node.rel = 'noopener noreferrer';
  }
  return node;
}

/** Where a source opens. A page of the person's papers opens the reader on
 *  that page, carrying the passages to highlight in the fragment
 *  (web/js/reader/reader.js); everything else opens where it lives. */
export function sourceUrl(source, quotes = []) {
  const url = source?.url ?? '';
  return url.startsWith('/library/') ? url + sourceHash(source, quotes) : url;
}

/** A citation number linking to its source, with the hover card. `[3]` in the
 *  prose and the small numbers on an overview are the same thing and are built
 *  the same way.
 *
 *  Falls back to position when no source carries the index, which only happens
 *  for a transcript saved before sources were numbered explicitly. */
export function citeLink(index, sources, className = 'cite', quotes = [], label = '') {
  const list = sources ?? [];
  const source = list.find((s) => s.index === index) ?? list[index - 1];
  if (!source) return null;
  const node = outbound(sourceUrl(source, quotes), className);
  node.dataset.cite = String(index);
  node.classList.add('cite-link');
  node.textContent = label || String(index);
  node.setAttribute('aria-label', source.title ? `Source ${index}: ${source.title}` : `Source ${index}`);
  // A passage citation, `[4: "exact words"]`: the number carries the words it
  // points to, shown on hover and highlighted when the page opens.
  if (quotes.length) {
    node.dataset.quotes = JSON.stringify(quotes);
    node.classList.add('passage');
    node.setAttribute('aria-label', `Source ${index}, passage: ${quotes.join(' … ')}`);
    return attachPreview(node, { ...source, quotes });
  }
  return attachPreview(node, source);
}
