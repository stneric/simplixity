// The redact tool's pure half: what the patterns catch, how terms merge, and
// what the copied text looks like. The output of `redactText` is the thing a
// person pastes somewhere else, so it is asserted whole.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  clock,
  detect,
  findAll,
  guessKind,
  labels,
  layout,
  luhn,
  mergeTerms,
  nameParts,
  redactText,
  remaining,
  sections,
  tally,
  validIban,
} from '../../web/js/tools/redact.js';

const CONTRACT = `Kaufvertrag zwischen Herrn Max Mustermann, Hauptstraße 12a, 72764 Reutlingen,
und der Beispiel GmbH (www.beispiel-gmbh.de, info@beispiel-gmbh.de, Tel. 07121 123456).
Fahrzeug: VW Golf, FIN WVWZZZ1JZXW000001, Kennzeichen RT-AB 123. Kundennummer: K-48213.
IBAN DE89 3704 0044 0532 0130 00. Der Kaufpreis beträgt 12.500,00 Euro, zahlbar am 01.03.2025.
Passwort: hunter2! Mustermann erhält die Schlüssel. Server 192.168.1.20. geb. 12.04.1980`;

const run = (text, findings = []) => {
  const terms = mergeTerms(findings, detect(text));
  const spans = layout(text, terms);
  return { terms, spans, out: redactText(text, spans, terms) };
};

test('a contract comes out with every identifying detail replaced', () => {
  const { out } = run(CONTRACT, [{ text: 'Beispiel GmbH', kind: 'org' }]);
  assert.equal(
    out,
    `Kaufvertrag zwischen Herrn [PERSON_1], [ADDRESS_1], [ADDRESS_2],
und der [ORG_1] ([URL_1], [EMAIL_1], Tel. [PHONE_1]).
Fahrzeug: VW Golf, FIN [VIN_1], Kennzeichen [ID_1]. Kundennummer: [ID_2].
IBAN [IBAN_1]. Der Kaufpreis beträgt 12.500,00 Euro, zahlbar am 01.03.2025.
Passwort: [SECRET_1] [PERSON_1] erhält die Schlüssel. Server [IP_1]. geb. [DATE_1]`,
  );
});

test('an ordinary date and an amount are left for the reviewer, not redacted', () => {
  const { terms } = run(CONTRACT);
  const date = terms.find((t) => t.text === '01.03.2025');
  assert.equal(date.origin, 'suspect');
  assert.equal(date.on, false);
  assert.ok(!terms.some((t) => t.text.includes('12.500')));
});

test('digits inside a sure hit do not become terms of their own', () => {
  const texts = detect(CONTRACT).map((h) => h.text);
  // The IBAN's digit groups look like a phone number, the postcode like an id.
  assert.ok(!texts.includes('0044 0532 0130 00'));
  assert.ok(!texts.includes('72764'));
});

test('checksums keep amounts out of IBANs and cards', () => {
  assert.ok(validIban('DE89 3704 0044 0532 0130 00'));
  assert.ok(!validIban('DE89 3704 0044 0532 0130 01'));
  assert.ok(luhn('4111 1111 1111 1111'));
  assert.ok(!luhn('4111 1111 1111 1112'));
  assert.deepEqual(detect('Summe 1234 5678 9012 3456 EUR').filter((h) => h.kind === 'card'), []);
});

test('the value after a label is the secret, not the label', () => {
  const hits = detect('API-Key: sk-abc123XYZ\nPIN = 4711');
  assert.deepEqual(
    hits.filter((h) => h.kind === 'secret').map((h) => h.text),
    ['sk-abc123XYZ', '4711'],
  );
});

test('a surname on its own takes the full name’s placeholder', () => {
  const text = 'Anna Schmidt kauft. Später zahlt Schmidt den Rest.';
  const { out } = run(text, [{ text: 'Anna Schmidt', kind: 'person' }]);
  assert.equal(out, '[PERSON_1] kauft. Später zahlt [PERSON_1] den Rest.');
});

test('name particles are not hidden on their own', () => {
  assert.deepEqual(nameParts({ kind: 'person', text: 'Dr. Ursula von der Leyen' }), [
    'Ursula',
    'Leyen',
  ]);
  assert.deepEqual(nameParts({ kind: 'org', text: 'Müller GmbH' }), []);
});

test('a term matches whole words only, ignoring case and line breaks', () => {
  assert.deepEqual(findAll('Ann und die Annahme', 'Ann'), [[0, 3]]);
  assert.deepEqual(findAll('Max\nMUSTERMANN', 'max mustermann'), [[0, 14]]);
});

test('placeholders are numbered per kind in reading order', () => {
  const text = 'Bob mailt bob@x.de, dann Eve. Bob erneut.';
  const { spans, terms } = run(text, [
    { text: 'Eve', kind: 'person' },
    { text: 'Bob', kind: 'person' },
  ]);
  const names = labels(spans, terms);
  assert.equal(names.get('bob'), 'PERSON_1');
  assert.equal(names.get('eve'), 'PERSON_2');
});

test('a kept term stays in the text, and a re-scan does not undo the decision', () => {
  const text = 'Anna Schmidt unterschreibt.';
  let terms = mergeTerms([{ text: 'Anna Schmidt', kind: 'person' }], []);
  terms[0].on = false;
  terms[0].decided = true;
  terms = mergeTerms([{ text: 'Anna Schmidt', kind: 'person' }], [], terms);
  const spans = layout(text, terms);
  assert.equal(redactText(text, spans, terms), text);
});

test('the longer span wins an overlap', () => {
  const text = 'Kontakt: info@beispiel.de';
  const { out } = run(text);
  assert.equal(out, 'Kontakt: [EMAIL_1]');
});

test('progress words: a clock, an estimate that waits for evidence, a tally', () => {
  assert.equal(clock(0), '0:00');
  assert.equal(clock(7_900), '0:07');
  assert.equal(clock(760_000), '12:40');

  assert.equal(remaining([], 5), null);
  assert.equal(remaining([10_000], 5), null);
  assert.equal(remaining([10_000, 20_000], 4), 60_000);
  assert.equal(remaining([10_000, 20_000], 0), null);

  assert.equal(tally([]), '');
  assert.equal(
    tally([
      { text: 'a@x.de', kind: 'email' },
      { text: 'Anna', kind: 'person' },
      { text: 'Bob', kind: 'person' },
    ]),
    'person ×2, email',
  );
});

test('the review panel groups terms by decision, in reading order, with counts', () => {
  const text = 'Eve trifft Bob. Bob zahlt am 01.03.2025. Eve lacht.';
  const terms = mergeTerms(
    [
      { text: 'Eve', kind: 'person' },
      { text: 'Bob', kind: 'person' },
    ],
    detect(text),
  );
  terms.find((t) => t.key === 'eve').on = false;
  terms.find((t) => t.key === 'eve').decided = true;
  const spans = layout(text, terms);
  const { check, redacted, kept, counts } = sections(terms, spans);
  assert.deepEqual(
    redacted.map((t) => t.text),
    ['Bob'],
  );
  assert.deepEqual(
    kept.map((t) => t.text),
    ['Eve'],
  );
  assert.deepEqual(
    check.map((t) => t.kind),
    ['date'],
  );
  assert.equal(counts.get('bob'), 2);
  assert.equal(counts.get('eve'), 2);
});

test('a selection gets a sensible first guess at its kind', () => {
  assert.equal(guessKind('Anna Schmidt'), 'person');
  assert.equal(guessKind('anna@example.org'), 'email');
  assert.equal(guessKind('www.example.org'), 'url');
  assert.equal(guessKind('+49 7121 123456'), 'phone');
  assert.equal(guessKind('Hauptstraße 12a'), 'address');
  assert.equal(guessKind('K-48213'), 'id');
  assert.equal(guessKind('Beispiel GmbH'), 'org');
  assert.equal(guessKind('vertraulich'), 'other');
  // German capitalises every noun, so one capitalised word is no name.
  assert.equal(guessKind('Kaufpreis'), 'other');
});
