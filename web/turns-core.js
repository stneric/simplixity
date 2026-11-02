// What is printed beside a turn, pure half: when a message was sent, which
// model wrote the answer, and which attached files went out with a question.
// No DOM, so `node --test` checks it, the same split as attachments-core.js.

/** A time in the browser's own zone and language: the hour alone for today,
 *  the date as well for anything older. `title` is the full moment, for the
 *  tooltip. Empty for a missing or unreadable time, which stored messages from
 *  before it was recorded have. */
export function stamp(iso, { now = new Date(), locale } = {}) {
  const at = iso instanceof Date ? iso : new Date(iso ?? NaN);
  if (!iso || Number.isNaN(at.getTime())) return { text: '', title: '' };
  const time = { hour: '2-digit', minute: '2-digit' };
  const today = at.toDateString() === now.toDateString();
  const sameYear = at.getFullYear() === now.getFullYear();
  const date = { day: 'numeric', month: 'short', ...(sameYear ? {} : { year: 'numeric' }) };
  return {
    text: at.toLocaleString(locale, today ? time : { ...date, ...time }),
    title: at.toLocaleString(locale, { dateStyle: 'full', timeStyle: 'short' }),
  };
}

/** The model's own name, without the provider or the vendor in front of it:
 *  `openrouter/anthropic/claude-sonnet` and `anthropic/claude-sonnet` are both
 *  `claude-sonnet`. The full id goes in the tooltip. A stored message carries
 *  `provider/model`, the live response only `model`, and this makes the two
 *  read the same. */
export function modelLabel(model) {
  const full = String(model ?? '').trim();
  if (!full) return '';
  return full.split('/').filter(Boolean).pop() ?? full;
}

/** Which of a chat's files went out with which question. The server records
 *  no link between them, only when each was made: a file belongs to the first
 *  question asked after it was attached. Files attached after the last
 *  question were never sent with one and belong in the composer still. Both
 *  times are the server's own ISO strings in UTC, so they compare as text. */
export function filesByMessage(messages, files) {
  const perMessage = messages.map(() => []);
  const draft = [];
  for (const file of files ?? []) {
    const at = messages.findIndex((message) => String(message.created_at ?? '') >= String(file.created_at ?? ''));
    if (at === -1) draft.push(file);
    else perMessage[at].push(file);
  }
  return { perMessage, draft };
}
