// Redact: everything between "the model found these strings" and the text that
// is copied out. No DOM here, so all of it is testable under `node --test`; the
// dialog that drives it is web/redact-view.js.
//
// The unit is a *term*, a string to hide wherever it occurs, not a position.
// A name the model found in excerpt 7 is just as identifying in excerpt 2 where
// it missed it, and a reviewer who restores "Müller" once means every "Müller".
// Positions are derived from terms on every change by `layout`, which is cheap
// next to a reviewer's click and keeps the state a plain list.
//
// A term is `{ key, text, kind, on, origin }`:
//   on      whether it is replaced in the output
//   origin  'model' | 'pattern' | 'manual' | 'suspect'
// Suspects are pattern hits that are only *possibly* identifying, a date, a
// bare five-digit number, so they are highlighted for a decision and left as
// they are until someone makes it.

/** What can be chosen when adding a term by hand. The model's vocabulary
 *  (app/tools/redact.py KINDS) plus `ip`, which only the patterns produce. */
export const KINDS = [
  'person',
  'org',
  'address',
  'email',
  'phone',
  'url',
  'iban',
  'card',
  'vin',
  'ip',
  'id',
  'secret',
  'date',
  'other',
];

export const keyOf = (text) =>
  String(text ?? '')
    .normalize('NFC')
    .replace(/\s+/g, ' ')
    .trim()
    .toLocaleLowerCase();

const WORD = /[\p{L}\p{N}]/u;

// ── checksums ────────────────────────────────────────────────────────
//
// A pattern that looks like an IBAN or a card number matches every long digit
// run in a contract, invoice totals included. The checksum is what separates
// the account from the amount, so a hit that passes it is redacted outright and
// one that fails it is not an IBAN at all.

export function validIban(raw) {
  const iban = raw.replace(/\s+/g, '').toUpperCase();
  if (!/^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$/.test(iban)) return false;
  const moved = iban.slice(4) + iban.slice(0, 4);
  let rest = 0;
  for (const char of moved) {
    const value = /\d/.test(char) ? char : String(char.charCodeAt(0) - 55);
    for (const digit of value) rest = (rest * 10 + Number(digit)) % 97;
  }
  return rest === 1;
}

export function luhn(raw) {
  const digits = raw.replace(/\D/g, '');
  if (digits.length < 13 || digits.length > 19) return false;
  let sum = 0;
  for (let i = 0; i < digits.length; i++) {
    let d = Number(digits[digits.length - 1 - i]);
    if (i % 2 === 1) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
  }
  return sum % 10 === 0;
}

const digitCount = (s) => (s.match(/\d/g) ?? []).length;

// ── patterns ─────────────────────────────────────────────────────────
//
// `sure` hits are replaced by default; the rest are suspects. `group` takes a
// capture instead of the whole match, for the labelled forms ("Passwort: x",
// "Kundennummer 123"), where the label is context and only the value is secret.
// German and English, because those are the documents this is used on.

const NAME = String.raw`\p{Lu}[\p{L}'’-]+`;
const STREET =
  'straße|strasse|str\\.|weg|gasse|platz|allee|ring|damm|ufer|chaussee|steig|pfad|markt|berg|hof';

export const PATTERNS = [
  { kind: 'email', sure: true, re: /[\p{L}\p{N}._%+-]+@[\p{L}\p{N}-]+(?:\.[\p{L}\p{N}-]+)*\.\p{L}{2,}/gu },
  { kind: 'url', sure: true, re: /\b(?:https?:\/\/|www\.)[^\s<>"'()[\]{}]+/giu },
  // Bare domains: "firma-xy.de" names the company as surely as its name does.
  {
    kind: 'url',
    sure: true,
    re: /\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:de|com|org|net|eu|io|at|ch|info|biz|co|uk|fr|nl|it|es|app|dev)\b(?:\/[^\s<>"']*)?/giu,
  },
  { kind: 'iban', sure: true, check: validIban, re: /\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b/g },
  { kind: 'card', sure: true, check: luhn, re: /\b\d(?:[ -]?\d){12,18}\b/g },
  // 17 characters without I, O or Q, and both letters and digits in it, a
  // plain 17-letter word is not a vehicle.
  {
    kind: 'vin',
    sure: true,
    check: (s) => /[A-Z]/.test(s) && digitCount(s) >= 2,
    re: /\b[A-HJ-NPR-Z0-9]{17}\b/g,
  },
  { kind: 'ip', sure: true, re: /\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b/g },
  {
    kind: 'phone',
    sure: true,
    check: (s) => digitCount(s) >= 7 && digitCount(s) <= 15,
    re: /(?:\+|\b00)\d{1,3}[\s/().-]*(?:\d[\s/().-]*){5,13}\d/g,
  },
  {
    kind: 'phone',
    sure: true,
    check: (s) => digitCount(s) >= 7 && digitCount(s) <= 15,
    re: /\b0\d{2,5}(?:[ /-]+\d{2,}){1,3}\b|\b0\d{6,12}\b/g,
  },
  {
    kind: 'secret',
    sure: true,
    group: 1,
    re: /\b(?:passwor[dt]|kennwort|passphrase|pin|puk|api[-_ ]?key|token|secret|zugangs(?:code|daten)|access[-_ ]?code|benutzer(?:name)?|username|login)\s*[:=]\s*(\S+)/giu,
  },
  {
    kind: 'id',
    sure: true,
    group: 1,
    re: /\b(?:kunden|vertrags|rechnungs|auftrags|bestell|mitglieds|personal|versicherungs|policen|steuer|sozialversicherungs|ausweis|pass|konto|referenz|vorgangs|akten|mandanten|kfz|fahrgestell)-?\s?(?:nummer|nr\.?|id|kennzeichen)\s*[:.]?\s*([A-Z0-9][A-Z0-9/.-]{2,})/giu,
  },
  {
    kind: 'id',
    sure: true,
    group: 1,
    re: /\b(?:customer|contract|invoice|order|account|policy|reference|case|tax|member|employee|passport|licen[cs]e)\s*(?:number|no\.?|id|#)\s*[:.]?\s*([A-Z0-9][A-Z0-9/.-]{2,})/giu,
  },
  {
    kind: 'id',
    sure: true,
    group: 1,
    re: /\b(?:(?:amtl\.\s*|Kfz-)?Kennzeichen|licen[cs]e plate|registration)\s*:?\s*(\p{Lu}{1,3}[- ]\p{Lu}{1,2}\s?\d{1,4}[EH]?)\b/gu,
  },
  { kind: 'id', sure: true, group: 1, re: /\b(?:Az\.|Aktenzeichen|USt-?Id(?:Nr)?\.?|Steuer-?ID)\s*:?\s*([A-Z0-9][A-Z0-9/. -]{3,}[A-Z0-9])/gu },
  // A title in front of a capitalised word is the strongest name signal plain
  // text has, and German capitalises every noun, so nothing weaker is usable.
  {
    kind: 'person',
    sure: true,
    group: 1,
    re: new RegExp(
      String.raw`\b(?:Herrn?|Frau|Mr\.?|Mrs\.?|Ms\.?|Dr\.|Prof\.)\s+((?:(?:Dr|Prof)\.\s+)?${NAME}(?:\s+(?:von|van|de|zu)\s+${NAME}|[ \t]+${NAME})?)`,
      'gu',
    ),
  },
  {
    kind: 'person',
    sure: true,
    group: 1,
    re: new RegExp(
      String.raw`\b(?:Name|Vorname|Nachname|Unterschrift|Signed|Inhaber(?:in)?|Ansprechpartner(?:in)?|Kontoinhaber(?:in)?|Contact)\s*:\s*(${NAME}(?:[ \t]+${NAME}){0,2})`,
      'gu',
    ),
  },
  { kind: 'address', sure: true, re: new RegExp(String.raw`\b\p{Lu}[\p{L}-]*(?:${STREET})\s+\d{1,4}\s?[a-zA-Z]?\b`, 'gu') },
  { kind: 'address', sure: true, re: /\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St\.|Road|Rd\.|Avenue|Ave\.|Lane|Drive|Way)\b/g },
  // Postcode and town. "12345 Euro" is an amount, not a place.
  {
    kind: 'address',
    sure: true,
    re: /\b\d{5}\s+(?!(?:Euro|EUR|Stück|Dollar|USD|Franken|CHF|Mal|Tage|Jahre|Monate)\b)\p{Lu}[\p{L}-]+(?:\s(?:am|an der|im|bei|ob der)\s\p{Lu}[\p{L}-]+)?/gu,
  },
  {
    kind: 'date',
    sure: true,
    group: 1,
    re: /\b(?:geb(?:oren)?\.?(?:\s+am)?|born(?:\s+on)?|DOB|date of birth|Geburtsdatum)\s*:?\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\.\s?\p{L}+\s\d{4})/giu,
  },

  // Suspects: highlighted, left alone until someone decides.
  { kind: 'date', sure: false, re: /\b\d{1,2}\.\s?\d{1,2}\.\s?(?:19|20)\d{2}\b|\b(?:19|20)\d{2}-\d{2}-\d{2}\b/gu },
  { kind: 'id', sure: false, re: /\b\p{Lu}{1,3}-\p{Lu}{1,2}\s?\d{1,4}[EH]?\b/gu },
  // Anything with five or more digits that nothing above explained: a postcode,
  // an order number, a personnel number. Amounts are written with separators
  // this does not cross ("1.234,56"), so they stay out.
  {
    kind: 'id',
    sure: false,
    check: (s) => digitCount(s) >= 5,
    re: /\b[A-Z]{0,4}[-/]?\d[\d/-]{3,}\d[A-Z]{0,2}\b/g,
  },
];

// Sentence punctuation a match swallows. Not stripped from secrets, where a
// trailing "!" is as likely to be part of the password as of the sentence.
const TRAILING = /[.,;:!?'")\]]+$/;

/**
 * Pattern hits in `text`, one per distinct string.
 *
 * A hit lying wholly inside an earlier *sure* hit is dropped: the digits of an
 * IBAN also look like a phone number, and the postcode inside "72764
 * Reutlingen" like a bare number. Laid out later the longer span would win
 * anyway, but as terms of their own they would go on to match elsewhere.
 * That is why PATTERNS runs most specific first.
 *
 * @returns {{text: string, kind: string, sure: boolean}[]}
 */
export function detect(text) {
  const found = new Map();
  const claimed = new Uint8Array(text.length);
  for (const pattern of PATTERNS) {
    pattern.re.lastIndex = 0;
    for (const match of text.matchAll(pattern.re)) {
      const raw = pattern.group ? match[pattern.group] : match[0];
      if (!raw) continue;
      const offset = match.index + (pattern.group ? match[0].indexOf(raw) : 0);
      let value = pattern.kind === 'secret' ? raw : raw.replace(TRAILING, '');
      const lead = value.length - value.trimStart().length;
      value = value.trim();
      if (value.length < 2) continue;
      if (pattern.check && !pattern.check(value)) continue;
      const start = offset + lead;
      const end = start + value.length;
      if (claimed.subarray(start, end).every(Boolean)) continue;
      if (pattern.sure) claimed.fill(1, start, end);
      const key = keyOf(value);
      const known = found.get(key);
      // First pattern wins the kind (they are ordered most specific first),
      // but a sure hit outranks a suspect one for the same string.
      if (!known) found.set(key, { text: value, kind: pattern.kind, sure: pattern.sure });
      else if (pattern.sure && !known.sure) known.sure = true;
    }
  }
  return [...found.values()];
}

// ── terms ────────────────────────────────────────────────────────────

const RANK = { manual: 3, model: 2, pattern: 1, suspect: 0 };

/**
 * Fold model findings and pattern hits into one term list. A string found by
 * both is one term: on if either said so, under the more specific kind.
 *
 * @param {{text: string, kind: string}[]} findings  from the local model
 * @param {{text: string, kind: string, sure: boolean}[]} hits  from `detect`
 */
export function mergeTerms(findings, hits, existing = []) {
  const terms = new Map(existing.map((t) => [t.key, { ...t }]));
  const add = (text, kind, origin) => {
    const key = keyOf(text);
    if (!key) return;
    const on = origin !== 'suspect';
    const known = terms.get(key);
    if (!known) {
      terms.set(key, { key, text: text.trim(), kind: KINDS.includes(kind) ? kind : 'other', on, origin });
      return;
    }
    // A decision someone made by hand is never overridden by a re-scan.
    if (known.origin === 'manual' || known.decided) return;
    if (on && !known.on) known.on = true;
    if (RANK[origin] > RANK[known.origin]) known.origin = origin;
    if ((known.kind === 'other' || known.kind === 'id') && kind !== 'other' && KINDS.includes(kind)) {
      known.kind = kind;
    }
  };
  for (const f of findings) add(f.text, f.kind, 'model');
  for (const h of hits) add(h.text, h.kind, h.sure ? 'pattern' : 'suspect');
  return [...terms.values()];
}

// Words that sit inside a name without identifying anyone on their own.
const NAME_PARTICLES = new Set(
  ['herr', 'herrn', 'frau', 'dr.', 'dr', 'prof.', 'prof', 'von', 'van', 'der', 'de', 'zu', 'mr', 'mr.', 'mrs', 'mrs.', 'ms', 'ms.'],
);

/** The parts of a person's name that are worth hiding on their own: a contract
 *  names "Anna Schmidt" once in the header and "Frau Schmidt" everywhere after,
 *  and the model, reading one excerpt at a time, is not reliably told twice. */
export function nameParts(term) {
  if (term.kind !== 'person') return [];
  return term.text
    .split(/\s+/)
    .map((part) => part.replace(/[.,;:]+$/, ''))
    .filter((part) => part.length >= 3 && /^\p{Lu}/u.test(part) && !NAME_PARTICLES.has(part.toLowerCase()));
}

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Every occurrence of `needle`, ignoring case and how whitespace is spelled,
 *  and only as a whole word: "Ann" must not take a bite out of "Annahme". */
export function findAll(text, needle) {
  const parts = needle.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return [];
  const re = new RegExp(parts.map(escape).join('\\s+'), 'giu');
  const out = [];
  for (const match of text.matchAll(re)) {
    const start = match.index;
    const end = start + match[0].length;
    const before = text[start - 1];
    const after = text[end];
    if (WORD.test(match[0][0]) && before && WORD.test(before)) continue;
    if (WORD.test(match[0].at(-1)) && after && WORD.test(after)) continue;
    out.push([start, end]);
  }
  return out;
}

/**
 * Where each term lands in `text`, as non-overlapping spans in reading order.
 *
 * Overlaps go to the longer span, so an e-mail address beats the domain inside
 * it and "Anna Schmidt" beats the "Schmidt" alias; at equal length a term that
 * is on beats one that is not, so a suspect never hides a redaction.
 *
 * @returns {{start: number, end: number, key: string, alias: boolean}[]}
 */
export function layout(text, terms) {
  const byKey = new Map(terms.map((t) => [t.key, t]));
  const candidates = [];
  for (const term of terms) {
    for (const [start, end] of findAll(text, term.text)) {
      candidates.push({ start, end, key: term.key, alias: false, on: term.on });
    }
    for (const part of nameParts(term)) {
      // A part that is a term of its own is that term's business.
      if (byKey.has(keyOf(part))) continue;
      for (const [start, end] of findAll(text, part)) {
        candidates.push({ start, end, key: term.key, alias: true, on: term.on });
      }
    }
  }
  candidates.sort(
    (a, b) => b.end - b.start - (a.end - a.start) || Number(b.on) - Number(a.on) || Number(a.alias) - Number(b.alias) || a.start - b.start,
  );
  const taken = new Uint8Array(text.length);
  const kept = [];
  for (const span of candidates) {
    let free = true;
    for (let i = span.start; i < span.end; i++) {
      if (taken[i]) {
        free = false;
        break;
      }
    }
    if (!free) continue;
    taken.fill(1, span.start, span.end);
    kept.push({ start: span.start, end: span.end, key: span.key, alias: span.alias });
  }
  return kept.sort((a, b) => a.start - b.start);
}

/** `[PERSON_1]`, `[IBAN_2]`: numbered per kind in order of first appearance,
 *  so the same person is the same placeholder throughout and a reader can
 *  still follow who did what to whom. */
export function labels(spans, terms) {
  const byKey = new Map(terms.map((t) => [t.key, t]));
  const counters = {};
  const out = new Map();
  for (const span of spans) {
    const term = byKey.get(span.key);
    if (!term?.on || out.has(term.key)) continue;
    counters[term.kind] = (counters[term.kind] ?? 0) + 1;
    out.set(term.key, `${term.kind.toUpperCase()}_${counters[term.kind]}`);
  }
  return out;
}

/** The text as it will be copied. */
export function redactText(text, spans, terms, names = labels(spans, terms)) {
  let out = '';
  let at = 0;
  for (const span of spans) {
    const label = names.get(span.key);
    if (!label) continue;
    out += text.slice(at, span.start) + `[${label}]`;
    at = span.end;
  }
  return out + text.slice(at);
}

// ── progress ─────────────────────────────────────────────────────────
//
// A local model takes anything from two seconds to two minutes per excerpt, and
// a progress bar that sits still for that long is indistinguishable from one
// that hung. These are the words the dialog uses to show that it has not.

/** A duration as "0:07" or "12:40". Minutes are not rolled into hours: a scan
 *  that long is one nobody is watching anyway. */
export function clock(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

/** Time left for `left` excerpts, from how long the finished ones took, or
 *  null until there is something to go by. Failures are included:
 *  a part that timed out took its two minutes, and the next one may too. One
 *  finished part is a guess, so the estimate waits for two. */
export function remaining(durations, left) {
  if (durations.length < 2 || left <= 0) return null;
  const mean = durations.reduce((a, b) => a + b, 0) / durations.length;
  return mean * left;
}

/** What one excerpt held, by kind and most frequent first: "person ×2, email". */
export function tally(findings) {
  const counts = new Map();
  for (const f of findings) counts.set(f.kind, (counts.get(f.kind) ?? 0) + 1);
  return [...counts]
    .sort((a, b) => b[1] - a[1])
    .map(([kind, n]) => (n > 1 ? `${kind} ×${n}` : kind))
    .join(', ');
}

// ── review ───────────────────────────────────────────────────────────

/**
 * The terms as the review panel lists them: what still needs a decision, what
 * is hidden, what was kept. Each in order of first appearance in the text, so
 * the list reads like the document does, with how often each one occurs; a
 * term that occurs nowhere (an alias swallowed by a longer span, say) is still
 * listed, last, since it can still be toggled.
 *
 * @returns {{check: object[], redacted: object[], kept: object[], counts: Map<string, number>}}
 */
export function sections(terms, spans) {
  const counts = new Map();
  const first = new Map();
  for (const [i, span] of spans.entries()) {
    counts.set(span.key, (counts.get(span.key) ?? 0) + 1);
    if (!first.has(span.key)) first.set(span.key, i);
  }
  const order = (a, b) => (first.get(a.key) ?? Infinity) - (first.get(b.key) ?? Infinity);
  const sorted = [...terms].sort(order);
  return {
    check: sorted.filter((t) => !t.on && t.origin === 'suspect' && !t.decided),
    redacted: sorted.filter((t) => t.on),
    kept: sorted.filter((t) => !t.on && !(t.origin === 'suspect' && !t.decided)),
    counts,
  };
}

/** A first guess at what a hand-selected string is, so redacting a name takes
 *  one click instead of two. Wrong guesses cost only the placeholder's name. */
export function guessKind(text) {
  const value = String(text ?? '').trim();
  if (/@/.test(value)) return 'email';
  if (/^(?:https?:\/\/|www\.)/i.test(value)) return 'url';
  if (/^\+?[\d\s()/-]{6,}$/.test(value)) return 'phone';
  if (/\d/.test(value) && /\p{L}/u.test(value) && /\b\d{1,4}[a-z]?\b/i.test(value) && value.includes(' ')) {
    return 'address';
  }
  if (/\d/.test(value)) return 'id';
  if (/\b(?:GmbH|AG|KG|e\.?V\.?|Inc\.?|Ltd\.?|LLC|SE)\b/.test(value)) return 'org';
  // Two or three capitalised words are most likely a name. One is not: in
  // German every noun is capitalised, and "Kaufpreis" offered as PERSON_4 is a
  // worse guess than no guess.
  if (/^\p{Lu}[\p{L}'’.-]*(?:\s+\p{Lu}[\p{L}'’.-]*){1,2}$/u.test(value)) return 'person';
  return 'other';
}
