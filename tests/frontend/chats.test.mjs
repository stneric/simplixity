// The sidebar's date buckets.
//
// The bug these pin: every chat sat under "Today". The buckets counted elapsed
// hours, so "Yesterday" meant "between 24 and 48 hours ago": a window that has
// nothing to do with what yesterday means. A chat from last night is fourteen
// hours old at breakfast and belongs under Yesterday, which is where the reader
// will look for it.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { installDom } from './dom.mjs';

installDom();
const { bucket } = await import('../../web/js/chat/chats.js');

/** Local midnight plus `hour`, `days` ago. The calendar case, unaffected by
 *  what time the test happens to run. Anchoring on wall-clock offsets instead
 *  would make these fail at 00:30, which is the very thing under test. */
function localTime(days, hour) {
  const date = new Date();
  date.setDate(date.getDate() - days);
  date.setHours(hour, 0, 0, 0);
  return date.toISOString();
}

test('just now is today', () => {
  assert.equal(bucket(new Date().toISOString()), 'Today');
});

test('last night is yesterday, not today', () => {
  // The case that was broken: 22:00 the previous day, read the next morning.
  assert.equal(bucket(localTime(1, 22)), 'Yesterday');
});

test('this morning is still today, however early', () => {
  assert.equal(bucket(localTime(0, 0)), 'Today');
});

test('the further buckets follow the same calendar', () => {
  assert.equal(bucket(localTime(3, 12)), 'Previous 7 days');
  assert.equal(bucket(localTime(10, 12)), 'Previous 30 days');
  assert.equal(bucket(localTime(60, 12)), 'Earlier');
});

test('a timestamp that cannot be read is filed last, not first', () => {
  // Chats written before `updated_at` existed carry the migration's empty
  // default; they are old by definition, and must never sort as "Today".
  assert.equal(bucket(''), 'Earlier');
  assert.equal(bucket('not a date'), 'Earlier');
  assert.equal(bucket(undefined), 'Earlier');
});
