// Tests for the sparkline layout:  node --test tests/frontend/
//
// `sparkline()` is a pure function from numbers to path data, split from the
// SVG for exactly this reason: the geometry can be checked without a browser.
//
// The property that matters is honesty about gaps. The server never asks a
// model to normalise a figure, so a series arrives as the strings a source
// wrote, and some of them will not parse. A straight segment drawn across a
// missing reading is an interpolation, and this app does not invent numbers to
// make a picture look finished.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { installDom, find } from './dom.mjs';

installDom();
const { sparkline, parseNumber, renderSparkline } = await import('../../web/js/render/spark.js');

const OPTS = { width: 100, height: 50, padding: 0 };

// ── parsing ──────────────────────────────────────────────────────────

test('reads a plain number', () => {
  assert.equal(parseNumber('42'), 42);
  assert.equal(parseNumber('-3.5'), -3.5);
});

test('reads both separator conventions, because both appear in the same app', () => {
  assert.equal(parseNumber('1,234.56'), 1234.56); // American
  assert.equal(parseNumber('1.234,56'), 1234.56); // German
  assert.equal(parseNumber('1.234'), 1234); // a thousands group, not 1.234
  assert.equal(parseNumber('3,5'), 3.5); // a German decimal
});

test('reads a number a source dressed up', () => {
  assert.equal(parseNumber('$302.25'), 302.25);
  assert.equal(parseNumber('24 °C'), 24);
  assert.equal(parseNumber('68%'), 68);
});

test('refuses what it cannot read rather than guessing', () => {
  for (const raw of ['', 'about three thousand', 'n/a', '—', null, undefined]) {
    assert.ok(Number.isNaN(parseNumber(raw)), String(raw));
  }
});

// ── layout ───────────────────────────────────────────────────────────

test('a monotone series produces one point per reading', () => {
  const model = sparkline([1, 2, 3, 4, 5], OPTS);
  assert.equal(model.points.length, 5);
  assert.equal(model.min, 1);
  assert.equal(model.max, 5);
});

test('the first and last points sit on the edges of the box', () => {
  const model = sparkline([1, 2, 3], OPTS);
  assert.equal(model.points[0].x, 0);
  assert.equal(model.points[2].x, 100);
});

test('y is inverted, because a bigger number belongs higher up', () => {
  const model = sparkline([0, 10], OPTS);
  assert.equal(model.points[0].y, 50); // the low reading is at the floor
  assert.equal(model.points[1].y, 0);
});

test('a flat series sits in the middle rather than on the floor', () => {
  // Pinned to the bottom it would read as zero, which is a different claim.
  const model = sparkline([7, 7, 7], OPTS);
  assert.deepEqual(
    model.points.map((p) => p.y),
    [25, 25, 25],
  );
});

test('a value that does not parse costs its point, not the chart', () => {
  const model = sparkline(['1', 'about 3k', '3', '4'], OPTS);
  assert.equal(model.points.length, 3);
  assert.deepEqual(
    model.points.map((p) => p.index),
    [0, 2, 3],
  );
});

test('the line breaks across a gap instead of interpolating through it', () => {
  const model = sparkline(['1', 'unknown', '3'], OPTS);
  // Two moves means two runs: the curve is drawn as two pieces, not one
  // straight segment standing in for a reading nobody has.
  assert.equal((model.d.match(/M /g) ?? []).length, 2);
});

test('a gap keeps the surviving points on their true position in time', () => {
  const model = sparkline(['1', 'unknown', '3'], OPTS);
  assert.deepEqual(
    model.points.map((p) => p.x),
    [0, 100],
  );
});

test('fewer than two readable points is no chart at all', () => {
  for (const values of [[], ['5'], ['x', 'y'], ['5', 'x']]) {
    assert.equal(sparkline(values, OPTS).d, '', JSON.stringify(values));
  }
});

test('the area closes to the floor so it can be filled', () => {
  const model = sparkline([1, 5, 3], OPTS);
  assert.ok(model.area.endsWith('Z'));
  assert.ok(model.area.includes('L 100 50')); // down to the baseline
});

// ── rendering ────────────────────────────────────────────────────────

test('rendering builds SVG nodes, never markup', () => {
  const svg = renderSparkline([1, 2, 3]);
  assert.equal(svg.tagName, 'SVG');
  assert.equal(svg.namespaceURI, 'http://www.w3.org/2000/svg');
  assert.equal(find(svg, 'path').length, 2); // area and line
  assert.equal(find(svg, 'circle').length, 1); // the latest reading
});

test('nothing to draw renders nothing rather than an empty box', () => {
  assert.equal(renderSparkline(['x']), null);
});

test('the curve is hidden from screen readers, because the table below is not', () => {
  assert.equal(renderSparkline([1, 2, 3]).getAttribute('aria-hidden'), 'true');
});
