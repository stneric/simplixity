// The composer's attachments, pure half: what may be attached, what a file's
// chip says, and what the next answer is written from. No DOM, so `node
// --test` checks it, the same split as reader.js and reader-core.js.
//
// A file attached to a chat makes the chat answer from that file
// (app/search/library.py), the way a literature project answers from its
// papers: only from it unless the Web switch is on, every sentence cited to a
// page, or to a part of a file that has no pages (app/tools/documents.py).

// What the file picker offers. The server decides from the bytes, this only
// keeps the picker from offering photos.
export const ACCEPT = '.pdf,.docx,.md,.markdown,.txt,.text,application/pdf,text/plain,text/markdown';

const SUFFIXES = ['.pdf', '.docx', '.md', '.markdown', '.txt', '.text'];

/** Whether a file looks attachable by its name. Dropped files arrive without
 *  the picker's filter, and a photo refused before it is uploaded costs the
 *  person nothing. */
export function accepted(name) {
  const lower = String(name ?? '').toLowerCase();
  return SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

/** An id for a chat that does not exist yet. Files are attached before the
 *  first question, under the id that question will then carry
 *  (`ChatRequest.chat_id`), so the browser has to pick it. */
export function newChatId() {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

const WARNINGS = {
  scanned: 'no text layer, nothing to search',
  thin_pages: 'some pages hold little text',
  columns: 'columns re-read in order',
};

/** The line under a file's name: how much of it there is, and anything that
 *  makes it search worse than it looks. */
export function fileSummary(file) {
  const count = Number(file?.pages) || 0;
  const pdf = (file?.kind ?? 'pdf') === 'pdf';
  const bits = [pdf ? `${count} page${count === 1 ? '' : 's'}` : `${count} part${count === 1 ? '' : 's'}`];
  const warnings = file?.warnings ?? [];
  for (const code of warnings) {
    if (WARNINGS[code]) bits.push(WARNINGS[code]);
  }
  if (!file?.chunks && !warnings.includes('scanned')) bits.push('nothing to search');
  else if (file.embedded != null && file.embedded < file.chunks) bits.push('keyword search');
  return bits.join(' · ');
}

/** Whether a file's chip should warn: it holds nothing the search can find. */
export function unusable(file) {
  return !file?.chunks || (file?.warnings ?? []).includes('scanned');
}

/** What the next answer is written from, shown when hovering the switch. */
export function filesBadge(count, web) {
  const what = `${count} attached file${count === 1 ? '' : 's'}`;
  return web ? `Answers from the ${what} and the web` : `Answers only from the ${what}`;
}

/** The sentence a failed upload shows on its chip. */
export function uploadError(status, detail) {
  if (detail) return String(detail);
  if (status === 413) return 'The file is too large.';
  if (status === 404) return 'This chat belongs to another profile.';
  return `The upload failed (HTTP ${status}).`;
}
