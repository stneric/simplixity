// Sparklines: a line over a series, with no axes and no grid.
//
// Same split as web/diagram.js, for the same reason. `sparkline()` is a pure
// function from numbers to path data (no DOM, so it can be tested with
// `node --test` and no browser), and `renderSparkline()` builds the SVG from
// it with createElementNS. No innerHTML anywhere near a value.
//
// **The numbers stay strings on the way here.** The server never asks a model
// to normalise a figure, because a model asked to normalise one is a model one
// step from computing one, so the widget carries the source's own text. Parsing
// is therefore a *rendering* concern and happens here, leniently: a value this
// cannot read costs its point on the curve and nothing else. The table below
// the chart still shows what the source wrote.

const DEFAULTS = { width: 280, height: 52, padding: 4 };

/**
 * Read a figure the way a source wrote it. NaN when it cannot be read.
 *
 * Both separator conventions appear in the same app (a German page writes
 * 1.234,56 and an American one writes 1,234.56), so neither can be assumed.
 * Whichever separator comes last is the decimal one; a lone separator with
 * exactly three digits behind it is a thousands group.
 */
export function parseNumber(text) {
  const raw = String(text ?? '').trim();

  // A value is a number only if it *begins* as one. A sign or a currency
  // symbol may lead it and a unit may trail it, but a word may not: stripping
  // the letters out of "about 3k" would silently plot a 3, which is the worst
  // available outcome: a point on the curve that no source ever stated.
  const match = raw.match(/^[$€£¥\s]*([-+−])?[$€£¥\s]*(\d[\d.,\s  ]*)/);
  if (!match) return NaN;

  const sign = match[1] === '-' || match[1] === '−' ? -1 : 1;
  const digits = match[2].replace(/[\s  ]/g, '').replace(/[.,]$/, '');
  const lastDot = digits.lastIndexOf('.');
  const lastComma = digits.lastIndexOf(',');
  let normalised = digits;

  if (lastDot >= 0 && lastComma >= 0) {
    // Both conventions appear in the same app (a German page writes 1.234,56
    // and an American one 1,234.56), so whichever separator comes last is the
    // decimal one and the other is grouping.
    const decimal = lastDot > lastComma ? '.' : ',';
    const grouping = decimal === '.' ? ',' : '.';
    normalised = digits.split(grouping).join('').replace(decimal, '.');
  } else if (/^[1-9]\d{0,2}([.,]\d{3})+$/.test(digits)) {
    // A lone separator with exactly three digits behind it, and no leading
    // zero: a thousands group in either convention. `0.123` is left alone,
    // because a leading zero is only ever a decimal.
    normalised = digits.replace(/[.,]/g, '');
  } else {
    normalised = digits.replace(',', '.');
  }

  const value = Number.parseFloat(normalised);
  return Number.isFinite(value) ? sign * value : NaN;
}

/**
 * Lay out a series. Pure, no DOM.
 *
 * `values` may contain anything; entries that do not parse become gaps rather
 * than being dropped, so the remaining points keep their true position on the
 * time axis. The line breaks across a gap instead of running through it,
 * a straight segment over a missing reading is an interpolation, and this app
 * does not invent numbers to make a picture look finished.
 *
 * @returns {{d: string, area: string, min: number|null, max: number|null,
 *            points: {x: number, y: number, index: number, value: number}[],
 *            width: number, height: number}}
 */
export function sparkline(values, options = {}) {
  const { width, height, padding } = { ...DEFAULTS, ...options };
  const list = Array.isArray(values) ? values : [];
  const empty = { d: '', area: '', min: null, max: null, points: [], width, height };

  const readable = list
    .map((value, index) => ({ index, value: typeof value === 'number' ? value : parseNumber(value) }))
    .filter((point) => Number.isFinite(point.value));
  if (readable.length < 2) return empty;

  const min = Math.min(...readable.map((p) => p.value));
  const max = Math.max(...readable.map((p) => p.value));
  const span = max - min;
  const innerW = Math.max(1, width - padding * 2);
  const innerH = Math.max(1, height - padding * 2);
  const stepX = list.length > 1 ? innerW / (list.length - 1) : 0;

  const points = readable.map((point) => ({
    index: point.index,
    value: point.value,
    x: round(padding + point.index * stepX),
    // A flat series sits in the middle of the box. Pinned to the floor it
    // would read as zero, which is a different claim entirely.
    y: round(span ? padding + innerH - ((point.value - min) / span) * innerH : padding + innerH / 2),
  }));

  // Contiguous runs, so a gap becomes a break in the line rather than a
  // straight segment across it.
  const runs = [];
  for (const point of points) {
    const current = runs[runs.length - 1];
    if (current && current[current.length - 1].index === point.index - 1) current.push(point);
    else runs.push([point]);
  }

  const floor = round(height - padding);
  const d = runs
    .map((run) => run.map((p, i) => `${i ? 'L' : 'M'} ${p.x} ${p.y}`).join(' '))
    .join(' ');
  const area = runs
    .filter((run) => run.length > 1)
    .map((run) => {
      const line = run.map((p, i) => `${i ? 'L' : 'M'} ${p.x} ${p.y}`).join(' ');
      return `${line} L ${run[run.length - 1].x} ${floor} L ${run[0].x} ${floor} Z`;
    })
    .join(' ');

  return { d, area, min, max, points, width, height };
}

function round(value) {
  return Math.round(value * 100) / 100;
}

// ── rendering ────────────────────────────────────────────────────────

const SVG_NS = 'http://www.w3.org/2000/svg';

const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  return node;
};

/** Draw a series. Returns an <svg>, or null when there is no curve to draw. */
export function renderSparkline(values, options = {}) {
  const model = sparkline(values, options);
  if (!model.d) return null;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${model.width} ${model.height}`,
    width: model.width,
    height: model.height,
    class: 'spark-svg',
    // The numbers are in the table directly below, so the curve is decoration
    // for a screen reader and says so rather than reading out coordinates.
    'aria-hidden': 'true',
    focusable: 'false',
  });
  svg.style.maxWidth = '100%';
  svg.style.height = 'auto';

  if (model.area) svg.append(svgEl('path', { d: model.area, class: 'spark-area' }));
  svg.append(svgEl('path', { d: model.d, class: 'spark-line' }));

  const last = model.points[model.points.length - 1];
  if (last) svg.append(svgEl('circle', { cx: last.x, cy: last.y, r: 2.5, class: 'spark-last' }));
  return svg;
}
