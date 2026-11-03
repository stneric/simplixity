// The composer's attachments, pure half: what may be attached, what a file's
// chip says, and what the switch tells the person the answer is written from.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { accepted, filesBadge, fileSummary, newChatId, unusable, uploadError } from '../../web/js/chat/attachments-core.js';

test('documents are accepted by name, photos are not', () => {
  for (const name of ['a.pdf', 'B.PDF', 'c.docx', 'd.md', 'e.markdown', 'f.txt']) assert.ok(accepted(name), name);
  for (const name of ['photo.png', 'sheet.xlsx', 'old.doc', 'noext', '', null]) assert.ok(!accepted(name), String(name));
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
