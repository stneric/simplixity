// The composer's attachments, pure half: what may be attached, what a file's
// chip says, and what the next answer is written from. No DOM, so `node
// --test` checks it, the same split as reader.js and reader-core.js.
//
// A file attached to a chat makes the chat answer from that file
// (app/search/library.py), the way a literature project answers from its
// papers: only from it unless the Web switch is on, every sentence cited to a
// page, or to a part of a file that has no pages (app/tools/documents.py).

// What the file picker offers. The server decides from the bytes, this only
// keeps the picker from offering what it would refuse.
export const ACCEPT =
  '.pdf,.docx,.md,.markdown,.txt,.text,application/pdf,text/plain,text/markdown,' +
  '.png,.jpg,.jpeg,.webp,.gif,image/png,image/jpeg,image/webp,image/gif';

const SUFFIXES = ['.pdf', '.docx', '.md', '.markdown', '.txt', '.text'];
const IMAGE_SUFFIXES = ['.png', '.jpg', '.jpeg', '.webp', '.gif'];
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];

/** Whether a file is a picture we take: by its type when the browser knows
 *  one, by its name otherwise. A HEIC from a phone is neither. */
export function isImage(file) {
  if (typeof file === 'string') file = { name: file };
  const type = String(file?.type ?? '').toLowerCase();
  if (type) return IMAGE_TYPES.includes(type);
  const lower = String(file?.name ?? '').toLowerCase();
  return IMAGE_SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

/** Whether a file looks attachable. Dropped files arrive without the picker's
 *  filter, and a file refused before it is uploaded costs the person nothing. */
export function accepted(file) {
  if (typeof file === 'string' || file == null) file = { name: file };
  if (isImage(file)) return true;
  const lower = String(file?.name ?? '').toLowerCase();
  return SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

export const REFUSED = 'Only images (PNG, JPEG, WebP, GIF), PDF, Word (.docx), Markdown and text files can be attached.';

// Larger pictures are scaled down in the browser before uploading. Providers
// scale anything bigger down themselves, so the extra pixels only cost upload
// time and the size limit.
export const MAX_IMAGE_SIDE = 2048;
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

/** The size a picture is scaled to, or null when it is fine as it is. */
export function scaledSize(width, height, bytes, max = MAX_IMAGE_SIDE) {
  const side = Math.max(width, height);
  if (side <= max && bytes <= MAX_IMAGE_BYTES) return null;
  const factor = Math.min(1, max / side);
  return { width: Math.max(1, Math.round(width * factor)), height: Math.max(1, Math.round(height * factor)) };
}

/** What the composer says when the chosen model cannot see the pictures in
 *  it. `vision` is the catalogue's answer: false is "cannot", null is "the
 *  provider does not say", which is not worth a warning. */
export function visionWarning(vision, model, count) {
  if (vision !== false || !count) return '';
  const what = count === 1 ? 'the image' : `the ${count} images`;
  return `${model || 'This model'} can’t see images, so ${what} would be left out. Pick a model with vision in Settings, or remove ${count === 1 ? 'it' : 'them'}.`;
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
 *  makes it search worse than it looks. A picture says what it is and how big. */
export function fileSummary(file) {
  if (file?.kind === 'image') return `Image · ${sizeLabel(file.size)}`;
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

export function sizeLabel(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

/** Whether a file's chip should warn: it holds nothing the search can find. */
export function unusable(file) {
  if (file?.kind === 'image') return false;
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
