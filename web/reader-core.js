// The reader's pure half: what a citation of a PDF page carries to the reader,
// and where on the rendered page a passage stands. No DOM, so `node --test`
// checks it, the same split as literature.js and literature-core.js.
//
// The passages travel in the URL fragment. The fragment never reaches a server
// or a log, the reader page stays the same file for every citation, and a
// copied link reopens on the same highlights.

/** The fragment a citation of a library page carries: the passage this very
 *  citation names (`q`, when the writer cited one), the exact words the
 *  citation check found carrying the answer's claims (`e`), and the passages
 *  the answer was written from (`p`). */
export function sourceHash(source, quotes = []) {
  const params = new URLSearchParams();
  for (const text of quotes) params.append('q', text);
  for (const text of source?.evidence ?? []) params.append('e', text);
  for (const text of source?.passages ?? []) params.append('p', text);
  const hash = params.toString();
  return hash ? `#${hash}` : '';
}

export function parseHash(hash) {
  const params = new URLSearchParams(String(hash ?? '').replace(/^#/, ''));
  return { cited: params.getAll('q'), evidence: params.getAll('e'), passages: params.getAll('p') };
}

/** Lowercase letters and digits, with a map from each kept character back to
 *  the text item it came from. PDF text items split words, hyphenate at line
 *  ends and drop spaces, and pypdf and pdf.js disagree on all three; letters
 *  and digits in order are what both agree on. */
function folded(items) {
  let text = '';
  const owner = [];
  items.forEach((item, index) => {
    for (const char of String(item ?? '').normalize('NFKD').toLowerCase()) {
      if (/[\p{L}\p{N}]/u.test(char)) {
        text += char;
        owner.push(index);
      }
    }
  });
  return { text, owner };
}

const fold = (text) => folded([text]).text;

// Below this a match proves nothing: "the model" is on every page.
const MIN_MATCH = 24;
// The window a passage falls back to when it is not on the page whole, in
// words. Long enough to be specific, short enough to survive an extraction
// difference a few words away.
const WINDOW_WORDS = 8;

/**
 * The text items of a page an excerpt covers, as a sorted array of indices.
 *
 * The whole excerpt first. Failing that, it is cut into overlapping windows
 * of eight words, and every window that is on the page marks its items: our passages
 * come from pypdf and the page from pdf.js, and where the two read a formula or
 * a ligature differently, the rest of the passage still lands where it is.
 * Returns [] when nothing of it is on the page, and the reader then says so.
 */
export function locate(items, excerpt) {
  const page = folded(items);
  const marked = new Set();
  const mark = (needle) => {
    if (needle.length < MIN_MATCH) return false;
    let at = page.text.indexOf(needle);
    if (at === -1) return false;
    while (at !== -1) {
      for (let i = at; i < at + needle.length; i += 1) marked.add(page.owner[i]);
      at = page.text.indexOf(needle, at + needle.length);
    }
    return true;
  };
  if (!mark(fold(excerpt))) {
    const words = String(excerpt ?? '').split(/\s+/).filter(Boolean);
    // Overlapping by half, so one misread word costs the two windows it sits
    // in and nothing else.
    for (let start = 0; start < words.length; start += WINDOW_WORDS / 2) {
      mark(fold(words.slice(start, start + WINDOW_WORDS).join(' ')));
    }
  }
  return [...marked].sort((a, b) => a - b);
}
