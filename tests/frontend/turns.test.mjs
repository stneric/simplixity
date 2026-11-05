// What is printed beside a turn: the time, the model, and the files a
// question went out with.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { filesByMessage, modelLabel, stamp } from '../../web/js/chat/turns-core.js';

test('today shows the hour, an older message the date too', () => {
  const now = new Date(2026, 9, 8, 15, 0);
  const today = stamp(new Date(2026, 9, 8, 9, 5), { now, locale: 'de-DE' });
  assert.equal(today.text, '09:05');
  assert.ok(today.title.includes('2026'));
  const older = stamp(new Date(2026, 9, 1, 9, 5), { now, locale: 'de-DE' });
  assert.ok(older.text.includes('09:05') && older.text.includes('1'), older.text);
  assert.ok(!older.text.includes('2026'), older.text);
  assert.ok(stamp(new Date(2025, 0, 1), { now, locale: 'de-DE' }).text.includes('2025'));
});

test('a missing time prints nothing', () => {
  assert.deepEqual(stamp(null), { text: '', title: '' });
  assert.deepEqual(stamp('not a date'), { text: '', title: '' });
});

test('the model is named without provider and vendor', () => {
  assert.equal(modelLabel('openrouter/anthropic/claude-sonnet-4.5'), 'claude-sonnet-4.5');
  assert.equal(modelLabel('anthropic/claude-sonnet-4.5'), 'claude-sonnet-4.5');
  assert.equal(modelLabel('ollama/llama3.1:8b'), 'llama3.1:8b');
  assert.equal(modelLabel(null), '');
});

test('a file goes with the first question asked after it was attached', () => {
  const messages = [{ created_at: '2026-10-08T10:00:00+00:00' }, { created_at: '2026-10-08T11:00:00+00:00' }];
  const files = [
    { id: 'a', created_at: '2026-10-08T09:59:00+00:00' },
    { id: 'b', created_at: '2026-10-08T10:30:00+00:00' },
    { id: 'c', created_at: '2026-10-08T10:31:00+00:00' },
    { id: 'd', created_at: '2026-10-08T12:00:00+00:00' },
  ];
  const { perMessage, draft } = filesByMessage(messages, files);
  assert.deepEqual(perMessage.map((list) => list.map((f) => f.id)), [['a'], ['b', 'c']]);
  // Attached after the last question: still waiting in the composer.
  assert.deepEqual(draft.map((f) => f.id), ['d']);
});

test('a picture goes with the question that claimed it, not by time', () => {
  const messages = [
    { message_id: 'm1', created_at: '2026-10-08T10:00:00+00:00' },
    { message_id: 'm2', created_at: '2026-10-08T11:00:00+00:00' },
  ];
  const files = [
    { id: 'p', kind: 'image', message_id: 'm2', created_at: '2026-10-08T09:00:00+00:00' },
    { id: 'q', kind: 'image', message_id: null, created_at: '2026-10-08T09:30:00+00:00' },
  ];
  const { perMessage, draft } = filesByMessage(messages, files);
  assert.deepEqual(perMessage.map((list) => list.map((f) => f.id)), [[], ['p']]);
  assert.deepEqual(draft.map((f) => f.id), ['q']);
});
