// Box-and-arrow diagrams, laid out and drawn here.
//
// Not Mermaid. Mermaid would handle more kinds of diagram, but it is a megabyte
// of parser for a syntax the model has to get exactly right, and the whole
// point of the widget layer is that the model supplies *data* against a schema,
// which a small model can do when emitting flawless Mermaid is beyond it. A
// validated node/edge list is the easiest thing to ask for and the safest thing
// to receive.
//
// Layout is a pure function over the graph (no DOM, so it can be tested), and
// rendering builds SVG nodes with createElementNS. No innerHTML anywhere near a
// label, same rule as the markdown renderer.

const NODE_MIN_W = 92;
const NODE_MAX_W = 190;
const NODE_H = 42;
const NODE_H_WRAPPED = 58;
const GAP_X = 22;
const GAP_Y = 54;
const PAD = 12;
const CHAR_W = 6.9; // 13px system sans, measured across mixed-case labels
const LABEL_PAD = 22;

/** Split a label to at most two lines, on a word boundary where one exists. */
function wrap(label, width) {
  const perLine = Math.max(6, Math.floor((width - LABEL_PAD) / CHAR_W));
  if (label.length <= perLine) return [label];

  const words = label.split(' ');
  const lines = [''];
  for (const word of words) {
    const line = lines[lines.length - 1];
    const candidate = line ? `${line} ${word}` : word;
    if (candidate.length <= perLine || !line) lines[lines.length - 1] = candidate;
    else lines.push(word);
    if (lines.length === 2 && lines[1].length > perLine) break;
  }
  // Two lines is the cap: a third makes the box taller than the row it sits in.
  if (lines.length > 2) lines.length = 2;
  if (lines[1] && lines[1].length > perLine) lines[1] = `${lines[1].slice(0, perLine - 1)}…`;
  return lines;
}

function boxWidth(label) {
  return Math.min(NODE_MAX_W, Math.max(NODE_MIN_W, label.length * CHAR_W + LABEL_PAD));
}

/**
 * Find the edges that close a cycle.
 *
 * A model asked to draw a process will sometimes draw a loop, and layering a
 * graph with a loop by relaxation never settles: each pass pushes the whole
 * cycle one row further down, which is how a four-box diagram ends up sixteen
 * rows tall. So the loop-closing edges are identified first, by depth-first
 * search: an edge into a node still on the stack is the one that closes the
 * cycle. They are excluded from layering and drawn as back edges instead.
 */
function backEdges(nodes, edges) {
  const outgoing = new Map(nodes.map((node) => [node.id, []]));
  edges.forEach((edge, index) => outgoing.get(edge.from)?.push({ edge, index }));

  const back = new Set();
  const done = new Set();
  const onStack = new Set();

  const visit = (id) => {
    onStack.add(id);
    for (const { edge, index } of outgoing.get(id) ?? []) {
      if (onStack.has(edge.to)) back.add(index);
      else if (!done.has(edge.to)) visit(edge.to);
    }
    onStack.delete(id);
    done.add(id);
  };

  for (const node of nodes) if (!done.has(node.id)) visit(node.id);
  return back;
}

/** Assign each node a layer: one past its deepest predecessor.
 *
 *  Forward edges only, so this is a DAG and the relaxation settles. */
function layers(nodes, forward) {
  const depth = new Map(nodes.map((node) => [node.id, 0]));
  for (let round = 0; round < nodes.length; round += 1) {
    let moved = false;
    for (const edge of forward) {
      const next = depth.get(edge.from) + 1;
      if (next > depth.get(edge.to)) {
        depth.set(edge.to, next);
        moved = true;
      }
    }
    if (!moved) break;
  }
  return depth;
}

/** Positions for every node and a size for the canvas. Pure, no DOM. */
export function layout(nodes, edges) {
  if (!nodes.length) return { nodes: [], edges: [], width: 0, height: 0 };

  const back = backEdges(nodes, edges);
  const forward = edges.filter((_edge, index) => !back.has(index));
  const depth = layers(nodes, forward);
  const rows = new Map();
  for (const node of nodes) {
    const row = depth.get(node.id) ?? 0;
    if (!rows.has(row)) rows.set(row, []);
    rows.get(row).push(node);
  }

  // Order each row by where its predecessors sit, so arrows cross as little as
  // possible. One pass: a full barycentre sweep is not worth it at this size.
  const placed = new Map();
  const ordered = [...rows.keys()].sort((a, b) => a - b);
  for (const row of ordered) {
    const members = rows.get(row);
    if (row > 0) {
      const centre = (node) => {
        const parents = forward
          .filter((edge) => edge.to === node.id && placed.has(edge.from))
          .map((edge) => placed.get(edge.from).x + placed.get(edge.from).w / 2);
        return parents.length ? parents.reduce((a, b) => a + b, 0) / parents.length : Infinity;
      };
      members.sort((a, b) => centre(a) - centre(b));
    }

    const widths = members.map((node) => boxWidth(node.label));
    const total = widths.reduce((a, b) => a + b, 0) + GAP_X * (members.length - 1);
    let x = -total / 2;
    members.forEach((node, index) => {
      const lines = wrap(node.label, widths[index]);
      placed.set(node.id, {
        id: node.id,
        label: node.label,
        lines,
        x,
        y: row * (NODE_H_WRAPPED + GAP_Y),
        w: widths[index],
        h: lines.length > 1 ? NODE_H_WRAPPED : NODE_H,
      });
      x += widths[index] + GAP_X;
    });
  }

  // Shift out of negative space and measure, now that everything is placed.
  const boxes = [...placed.values()];
  const minX = Math.min(...boxes.map((box) => box.x));
  for (const box of boxes) box.x -= minX - PAD;

  const routed = edges
    .filter((edge) => placed.has(edge.from) && placed.has(edge.to))
    .map((edge) => route(placed.get(edge.from), placed.get(edge.to), edge.label));

  // A back edge is routed around the right-hand side, past every box, so the
  // canvas has to be measured after routing or the loop is drawn off the edge.
  const rightmost = Math.max(
    ...boxes.map((box) => box.x + box.w),
    ...routed.filter((edge) => edge.back).map((edge) => edge.labelX + 14),
  );
  const width = rightmost + PAD;
  const height = Math.max(...boxes.map((box) => box.y + box.h)) + PAD;

  return { nodes: boxes, edges: routed, width, height: height + PAD };
}

/** The path between two boxes, plus where its label sits. */
function route(from, to, label) {
  const fromCx = from.x + from.w / 2;
  const toCx = to.x + to.w / 2;
  const forward = to.y > from.y;

  if (forward) {
    const y1 = from.y + from.h;
    const y2 = to.y;
    const mid = (y1 + y2) / 2;
    return {
      label,
      // A vertical stub out of each box then a single bend, so the arrow leaves
      // and arrives square rather than clipping the corner of a neighbour.
      d: `M ${fromCx} ${y1} C ${fromCx} ${mid}, ${toCx} ${mid}, ${toCx} ${y2}`,
      labelX: (fromCx + toCx) / 2,
      labelY: mid,
      back: false,
    };
  }

  // A back edge: a loop the model drew. It goes around the outside, on the
  // right, so it never runs through the boxes it is skipping past.
  const side = Math.max(from.x + from.w, to.x + to.w) + 26;
  const y1 = from.y + from.h / 2;
  const y2 = to.y + to.h / 2;
  return {
    label,
    d: `M ${from.x + from.w} ${y1} C ${side} ${y1}, ${side} ${y2}, ${to.x + to.w} ${y2}`,
    labelX: side,
    labelY: (y1 + y2) / 2,
    back: true,
  };
}

// ── rendering ────────────────────────────────────────────────────────

const SVG_NS = 'http://www.w3.org/2000/svg';

const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  return node;
};

/** Draw a validated diagram. Returns an <svg>, or null if there is nothing. */
export function renderDiagram(data) {
  const nodes = data?.nodes ?? [];
  const edges = (data?.edges ?? []).map((edge) => ({
    from: edge.from,
    to: edge.to,
    label: edge.label ?? '',
  }));
  if (nodes.length < 2 || !edges.length) return null;

  const model = layout(nodes, edges);
  const svg = svgEl('svg', {
    // Scales down on a phone and never overflows: the box is the answer column.
    viewBox: `0 0 ${model.width} ${model.height}`,
    width: model.width,
    height: model.height,
    class: 'diagram-svg',
    role: 'img',
    'aria-label': data.title || 'Diagram',
  });
  svg.style.maxWidth = '100%';
  svg.style.height = 'auto';

  const defs = svgEl('defs');
  const marker = svgEl('marker', {
    id: `arrow-${Math.random().toString(36).slice(2, 8)}`,
    viewBox: '0 0 10 10',
    refX: 9,
    refY: 5,
    markerWidth: 6,
    markerHeight: 6,
    orient: 'auto-start-reverse',
  });
  marker.append(svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', class: 'diagram-arrowhead' }));
  defs.append(marker);
  svg.append(defs);

  for (const edge of model.edges) {
    svg.append(
      svgEl('path', {
        d: edge.d,
        class: `diagram-edge${edge.back ? ' back' : ''}`,
        'marker-end': `url(#${marker.getAttribute('id')})`,
      }),
    );
    if (edge.label) {
      // A plate behind the label so the line does not read through the text.
      const width = edge.label.length * 5.8 + 10;
      svg.append(
        svgEl('rect', {
          x: edge.labelX - width / 2,
          y: edge.labelY - 8,
          width,
          height: 16,
          rx: 4,
          class: 'diagram-labelplate',
        }),
      );
      const text = svgEl('text', {
        x: edge.labelX,
        y: edge.labelY + 4,
        class: 'diagram-edgelabel',
        'text-anchor': 'middle',
      });
      text.textContent = edge.label;
      svg.append(text);
    }
  }

  for (const box of model.nodes) {
    svg.append(
      svgEl('rect', {
        x: box.x,
        y: box.y,
        width: box.w,
        height: box.h,
        rx: 9,
        class: 'diagram-node',
      }),
    );
    const first = box.lines.length > 1 ? box.y + box.h / 2 - 4 : box.y + box.h / 2 + 4;
    box.lines.forEach((line, index) => {
      const text = svgEl('text', {
        x: box.x + box.w / 2,
        y: first + index * 15,
        class: 'diagram-nodelabel',
        'text-anchor': 'middle',
      });
      text.textContent = line;
      svg.append(text);
    });
  }

  return svg;
}
