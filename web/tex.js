// TeX → DOM for the math models put in answers.
//
// Deliberately not KaTeX: the CSP allows no CDN, and like the markdown parser
// around it this renderer builds nodes with createElement and textContent and
// never assigns innerHTML, so model output cannot become markup here either.
// It covers what answers actually contain: Greek letters and symbols,
// sub/superscripts, accents, blackboard and script letters, fractions and
// roots. A command it does not know is shown literally rather than dropped,
// nothing the model wrote may silently vanish.

const SYMBOLS = {
  // Greek
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ϵ', varepsilon: 'ε',
  zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ',
  lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', varpi: 'ϖ', rho: 'ρ',
  varrho: 'ϱ', sigma: 'σ', varsigma: 'ς', tau: 'τ', upsilon: 'υ', phi: 'ϕ',
  varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π',
  Sigma: 'Σ', Upsilon: 'Υ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
  // Sets and logic
  in: '∈', notin: '∉', ni: '∋', subset: '⊂', supset: '⊃', subseteq: '⊆',
  supseteq: '⊇', nsubseteq: '⊈', cup: '∪', cap: '∩', setminus: '∖',
  emptyset: '∅', varnothing: '∅', forall: '∀', exists: '∃', nexists: '∄',
  neg: '¬', lnot: '¬', land: '∧', wedge: '∧', lor: '∨', vee: '∨',
  implies: '⟹', impliedby: '⟸', iff: '⟺', top: '⊤', bot: '⊥',
  vdash: '⊢', models: '⊨', therefore: '∴', because: '∵',
  // Relations
  leq: '≤', le: '≤', geq: '≥', ge: '≥', neq: '≠', ne: '≠', equiv: '≡',
  approx: '≈', approxeq: '≊', cong: '≅', sim: '∼', simeq: '≃', propto: '∝',
  ll: '≪', gg: '≫', prec: '≺', succ: '≻', perp: '⊥', parallel: '∥',
  nparallel: '∦', mid: '∣', asymp: '≍', doteq: '≐', triangleq: '≜',
  coloneqq: '≔', angle: '∠', triangle: '△',
  // Operators
  times: '×', div: '÷', cdot: '·', pm: '±', mp: '∓', ast: '∗', star: '⋆',
  circ: '∘', bullet: '∙', oplus: '⊕', ominus: '⊖', otimes: '⊗', odot: '⊙',
  dagger: '†', dag: '†', ddagger: '‡', ddag: '‡', wr: '≀', amalg: '⨿',
  // Arrows
  to: '→', gets: '←', mapsto: '↦', rightarrow: '→', leftarrow: '←',
  longrightarrow: '⟶', longleftarrow: '⟵', Rightarrow: '⇒', Leftarrow: '⇐',
  leftrightarrow: '↔', Leftrightarrow: '⇔', uparrow: '↑', downarrow: '↓',
  hookrightarrow: '↪', rightleftharpoons: '⇌',
  // Large operators
  sum: '∑', prod: '∏', coprod: '∐', int: '∫', iint: '∬', iiint: '∭',
  oint: '∮', bigcup: '⋃', bigcap: '⋂', bigvee: '⋁', bigwedge: '⋀',
  bigoplus: '⨁', bigotimes: '⨂', bigodot: '⨀', bigsqcup: '⨆',
  // Miscellaneous
  infty: '∞', partial: '∂', nabla: '∇', hbar: 'ℏ', hslash: 'ℏ', ell: 'ℓ',
  Re: 'ℜ', Im: 'ℑ', aleph: 'ℵ', wp: '℘', surd: '√', prime: '′',
  backprime: '‵', ldots: '…', cdots: '⋯', dots: '…', vdots: '⋮',
  ddots: '⋱', degree: '°', S: '§', P: '¶', copyright: '©', pounds: '£',
  checkmark: '✓', square: '□', blacksquare: '■', Diamond: '◇',
  diamond: '⋄', langle: '⟨', rangle: '⟩', lceil: '⌈', rceil: '⌉',
  lfloor: '⌊', rfloor: '⌋', Vert: '‖', vert: '∣', backslash: '\\',
};

const BLACKBOARD = {
  R: 'ℝ', N: 'ℕ', Z: 'ℤ', Q: 'ℚ', C: 'ℂ', H: 'ℍ', P: 'ℙ', E: '𝔼',
  F: '𝔽', D: '𝔻', K: '𝕂', T: '𝕋', B: '𝔹', S: '𝕊', O: '𝕆', I: '𝕀', J: '𝕁',
};

const SCRIPT = {
  A: '𝒜', B: 'ℬ', C: '𝒞', D: '𝒟', E: 'ℰ', F: 'ℱ', G: '𝒢', H: 'ℋ',
  I: 'ℐ', J: '𝒥', K: '𝒦', L: 'ℒ', M: 'ℳ', N: '𝒩', O: '𝒪', P: '𝒫',
  Q: '𝒬', R: 'ℛ', S: '𝒮', T: '𝒯', U: '𝒰', V: '𝒱', W: '𝒲', X: '𝒳',
  Y: '𝒴', Z: '𝒵',
};

// Combining diacritics, appended after the base character.
const ACCENTS = {
  hat: '̂', widehat: '̂', tilde: '̃', widetilde: '̃',
  bar: '̄', overline: '̅', vec: '⃗', dot: '̇', ddot: '̈',
};

// Rendered upright, the way TeX sets operator names.
const FUNCTIONS = new Set([
  'log', 'lg', 'ln', 'exp', 'sin', 'cos', 'tan', 'cot', 'sec', 'csc',
  'arcsin', 'arccos', 'arctan', 'sinh', 'cosh', 'tanh', 'coth', 'min', 'max',
  'sup', 'inf', 'lim', 'limsup', 'liminf', 'arg', 'det', 'dim', 'ker', 'deg',
  'gcd', 'lcm', 'Pr', 'mod', 'bmod', 'sgn', 'tr', 'rank', 'diag', 'span',
]);

// \ followed by a non-letter escapes that one character.
const ESCAPES = {
  $: '$', '%': '%', '&': '&', '#': '#', _: '_', '{': '{', '}': '}',
  '\\': '\\', ',': ' ', ';': ' ', ':': ' ', '!': '', ' ': ' ',
};

/** Render one TeX string into a span (inline) or div (display). */
export function renderMath(tex, { display = false } = {}) {
  const node = document.createElement(display ? 'div' : 'span');
  node.className = display ? 'tex tex-display' : 'tex';
  node.append(...parse({ tex: String(tex ?? ''), i: 0 }, null));
  return node;
}

// The parser is a plain index walk with one-character groups; every branch
// either consumes input or returns, so a half-streamed or malformed formula
// renders what it has rather than spinning.
function parse(state, stop) {
  const nodes = [];
  let text = '';
  const flush = () => {
    if (text) nodes.push(document.createTextNode(text));
    text = '';
  };

  while (state.i < state.tex.length) {
    const ch = state.tex[state.i];
    if (ch === stop) {
      state.i += 1;
      break;
    }
    if (ch === '}') {
      state.i += 1; // unmatched close: skip it, never spin on it
      continue;
    }
    if (ch === '{') {
      state.i += 1;
      flush();
      nodes.push(...parse(state, '}'));
      continue;
    }
    if (ch === '^' || ch === '_') {
      state.i += 1;
      const node = document.createElement(ch === '^' ? 'sup' : 'sub');
      node.append(...readArg(state));
      flush();
      nodes.push(node);
      continue;
    }
    if (ch === '\\') {
      flush();
      nodes.push(...command(state));
      continue;
    }
    text += ch;
    state.i += 1;
  }
  flush();
  return nodes;
}

/** One `{group}`, one command, or one character: what ^, _ and the
 *  argument-taking commands all consume. */
function readArg(state) {
  const ch = state.tex[state.i];
  if (ch === '{') {
    state.i += 1;
    return parse(state, '}');
  }
  if (ch === '\\') return command(state);
  if (ch == null) return [];
  state.i += 1;
  return [document.createTextNode(ch)];
}

function command(state) {
  state.i += 1; // the backslash itself
  const name = /^[a-zA-Z]+/.exec(state.tex.slice(state.i))?.[0];
  if (!name) {
    const ch = state.tex[state.i] ?? '';
    if (ch) state.i += 1;
    return [document.createTextNode(ESCAPES[ch] ?? ch)];
  }
  state.i += name.length;
  // TeX eats one space after a control word, but for symbols and operator
  // names that space is the only separator this renderer has ("x \in \mathbb{R}"
  // should not collapse into "x ∈ℝ"), so it is kept. For argument-taking
  // commands ("\hat x") it is structural and stays eaten.
  const hadSpace = state.tex[state.i] === ' ';
  if (hadSpace) state.i += 1;
  const gap = hadSpace ? [document.createTextNode(' ')] : [];

  if (name in SYMBOLS) return [document.createTextNode(SYMBOLS[name]), ...gap];
  if (FUNCTIONS.has(name)) return [styled('tex-up', name), ...gap];

  if (name in ACCENTS) {
    const text = textOf(readArg(state));
    return [
      document.createTextNode([...text].map((c) => (c.trim() ? c + ACCENTS[name] : c)).join('')),
    ];
  }

  if (name === 'frac' || name === 'dfrac' || name === 'tfrac') {
    const node = span('tex-frac');
    const num = span('tex-num');
    num.append(...readArg(state));
    const den = span('tex-den');
    den.append(...readArg(state));
    node.append(num, den);
    return [node];
  }

  if (name === 'sqrt') {
    const node = span('tex-sqrt');
    if (state.tex[state.i] === '[') {
      state.i += 1;
      const index = document.createElement('sup');
      index.append(...parse(state, ']'));
      node.append(index);
    }
    node.append(document.createTextNode('√'));
    const radicand = span('tex-radicand');
    radicand.append(...readArg(state));
    node.append(radicand);
    return [node];
  }

  if (name === 'mathbb' || name === 'mathcal') {
    const map = name === 'mathbb' ? BLACKBOARD : SCRIPT;
    return [
      document.createTextNode([...textOf(readArg(state))].map((c) => map[c] ?? c).join('')),
    ];
  }
  if (name === 'mathrm' || name === 'text' || name === 'operatorname') {
    return [styledArgs('tex-up', state)];
  }
  if (name === 'mathbf' || name === 'textbf' || name === 'bm') {
    return [styledArgs('tex-bf', state)];
  }
  if (name === 'mathit' || name === 'textit' || name === 'emph') return readArg(state);

  // Sizing commands keep their delimiter and lose the size machinery.
  if (/^(left|right|middle|[bB]igg?[lrm]?)$/.test(name)) {
    if (state.tex[state.i] === '\\') return command(state);
    const ch = state.tex[state.i] ?? '';
    if (ch) state.i += 1;
    return [document.createTextNode(ch === '.' ? '' : ch)];
  }
  if (name === 'quad') return [document.createTextNode(' ')];
  if (name === 'qquad') return [document.createTextNode('  ')];
  if (name === 'displaystyle' || name === 'limits' || name === 'nolimits') return [];

  // Unknown: show it rather than swallow it.
  return [document.createTextNode(`\\${name}`)];
}

const span = (className) => {
  const node = document.createElement('span');
  node.className = className;
  return node;
};

const styled = (className, text) => {
  const node = span(className);
  node.textContent = text;
  return node;
};

const styledArgs = (className, state) => {
  const node = span(className);
  node.append(...readArg(state));
  return node;
};

const textOf = (nodes) => nodes.map((node) => node.textContent ?? '').join('');
