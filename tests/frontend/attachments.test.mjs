// The composer's attachments, pure half: what may be attached, what a file's
// chip says, and what the switch tells the person the answer is written from.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  accepted,
  filesBadge,
  fileSummary,
  isImage,
  newChatId,
  scaledSize,
  unusable,
  uploadError,
  visionWarning,
} from '../../web/js/chat/attachments-core.js';

test('documents and pictures are accepted, everything else is not', () => {
  for (const name of ['a.pdf', 'B.PDF', 'c.docx', 'd.md', 'e.markdown', 'f.txt', 'photo.png', 'x.JPG', 'y.webp', 'z.gif'])
    assert.ok(accepted(name), name);
  for (const name of ['sheet.xlsx', 'old.doc', 'phone.heic', 'noext', '', null]) assert.ok(!accepted(name), String(name));
});

test('a picture is known by its type before its name', () => {
  assert.ok(isImage({ name: 'clipboard', type: 'image/png' }));
  assert.ok(!isImage({ name: 'photo.png', type: 'image/heic' }));
  assert.ok(isImage({ name: 'photo.jpeg', type: '' }));
  assert.ok(!isImage({ name: 'paper.pdf', type: 'application/pdf' }));
});

test('a large picture is scaled to its longest side, a small one is left alone', () => {
  assert.equal(scaledSize(1200, 800, 300_000), null);
  assert.deepEqual(scaledSize(4096, 2048, 3_000_000), { width: 2048, height: 1024 });
  assert.deepEqual(scaledSize(1000, 1000, 9_000_000), { width: 1000, height: 1000 });
});

test('a model without vision is warned about, an unknown one is not', () => {
  assert.match(visionWarning(false, 'Llama 3', 1), /^Llama 3 can’t see images, so the image/);
  assert.match(visionWarning(false, 'Llama 3', 3), /the 3 images/);
  assert.equal(visionWarning(null, 'Groq model', 2), '');
  assert.equal(visionWarning(true, 'GPT', 2), '');
  assert.equal(visionWarning(false, 'Llama 3', 0), '');
});

test('a picture says it is one', () => {
  assert.equal(fileSummary({ kind: 'image', size: 2_500_000 }), 'Image · 2.4 MB');
  assert.ok(!unusable({ kind: 'image', size: 10 }));
});

test('a new chat id is one the server accepts', () => {
  const id = newChatId();
  assert.match(id, /^[0-9a-f]{32}$/);
  assert.notEqual(id, newChatId());
});

test('a PDF counts pages, anything else parts', () => {
  assert.equal(fileSummary({ kind: 'pdf', pages: 12, chunks: 30, embedded: 30 }), '12 pages');
  assert.equal(fileSummary({ kind: 'docx', pages: 1, chunks: 2, embedded: 2 }), '1 part');
  assert.equal(fileSummary({ kind: 'txt', pages: 3, chunks: 5, embedded: 0 }), '3 parts · keyword search');
});

test('a scan says it holds nothing to search, and warns', () => {
  const scan = { kind: 'pdf', pages: 4, chunks: 0, warnings: ['scanned'] };
  assert.equal(fileSummary(scan), '4 pages · no text layer, nothing to search');
  assert.ok(unusable(scan));
  assert.ok(!unusable({ kind: 'pdf', pages: 4, chunks: 9, warnings: [] }));
});

test('the switch says what the answer is written from', () => {
  assert.equal(filesBadge(1, false), 'Answers only from the 1 attached file');
  assert.equal(filesBadge(2, true), 'Answers from the 2 attached files and the web');
});

test('a failed upload says why, in the servers words when there are some', () => {
  assert.equal(uploadError(422, 'This is not a PDF.'), 'This is not a PDF.');
  assert.equal(uploadError(413), 'The file is too large.');
  assert.match(uploadError(500), /HTTP 500/);
});
