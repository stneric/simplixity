// The redact tool's dialog: paste or open a document, scan it excerpt by
// excerpt on a local model, review what was found, copy the result.
//
// The browser drives the excerpts one request at a time (see
// app/tools/redact.py for why the server keeps nothing between them), so
// progress, stopping and retrying a failed excerpt are all just this loop.
//
// The review pane shows the *original* text with every span marked, rather than
// the output with placeholders in it. That way what is selected in it is the
// real text, so selecting a missed name and pressing Redact adds exactly that
// string, and a marked span still shows what it hides. The placeholder each
// one becomes is drawn by CSS from `data-label`, and generated content is not
// part of a selection.
//
// Beside it sits the term panel, the same decisions as a list. Reading a
// forty-page contract for yellow underlines is how a suspect gets missed; a
// list of "3 to check" with a Next button is how it does not. Both views edit
// the one term list, and every edit goes through `commit`, which is what makes
// undo a stack of snapshots rather than an inverse for every kind of change.
//
// Like the markdown renderer, nothing here assigns innerHTML: the document is
// untrusted text and goes in through textContent only.

import { iconNode } from '../shared/icons.js';
import { confirmDialog } from '../shared/dialog.js';
import {
  KINDS,
  clock,
  detect,
  findAll,
  guessKind,
  keyOf,
  labels,
  layout,
  mergeTerms,
  redactText,
  remaining,
  sections,
  tally,
} from './redact.js';

function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function button(label, className = 'confirm-btn', icon = '') {
  const node = el('button', className);
  node.type = 'button';
  if (icon) node.append(iconNode(icon, 15));
  node.append(document.createTextNode(label));
  return node;
}

function iconButton(icon, label) {
  const node = el('button', 'icon-btn');
  node.type = 'button';
  node.title = label;
  node.setAttribute('aria-label', label);
  node.append(iconNode(icon, 16));
  return node;
}

function kindSelect(value, label = 'Kind') {
  const select = el('select', 'select');
  select.setAttribute('aria-label', label);
  for (const kind of KINDS) {
    const option = el('option', '', kind);
    option.value = kind;
    select.append(option);
  }
  select.value = value;
  return select;
}

/** JSON in, `{ ok, status, data, message }` out, never a throw. */
async function post(url, body, { signal, raw = false } = {}) {
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': raw ? 'application/pdf' : 'application/json' },
      body: raw ? body : JSON.stringify(body),
      signal,
    });
    const data = await res.json().catch(() => ({}));
    const detail = typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`;
    return { ok: res.ok, status: res.status, data, message: res.ok ? '' : detail };
  } catch (error) {
    if (error?.name === 'AbortError') return { ok: false, status: 0, aborted: true, message: 'Stopped' };
    return { ok: false, status: 0, data: {}, message: 'The server is not answering.' };
  }
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
// Typing in a field must keep its own undo; the editor's undo is for the terms.
const isField = (node) => node?.closest?.('input, textarea, select, [contenteditable="true"]');

/**
 * Open the tool.
 * @param {{settings: import('../app/settings.js').Settings}} ctx
 */
export function openRedact({ settings }) {
  const state = {
    text: '',
    chunks: [],
    terms: [],
    failed: [], // { index, start, end, message }
    running: null, // AbortController while scanning
    outputEdited: false,
    model: null, // { provider, id }
    undo: [], // snapshots of `terms`, newest last
    redo: [],
    filter: '',
  };

  // ── shell ──────────────────────────────────────────────────────────

  const dialog = el('dialog', 'tool-dialog');
  const head = el('header', 'tool-head');
  const title = el('h2', 'tool-title');
  title.append(iconNode('redact', 18), document.createTextNode('Redact'));
  const modelSelect = el('select', 'select tool-model');
  modelSelect.setAttribute('aria-label', 'Local model');
  const close = el('button', 'icon-btn');
  close.type = 'button';
  close.title = 'Close';
  close.setAttribute('aria-label', 'Close');
  close.append(iconNode('close', 17));
  head.append(title, el('span', 'tool-badge', 'Local model'), modelSelect, close);

  const body = el('div', 'tool-body');
  dialog.append(head, body);

  // ── step 1: input ──────────────────────────────────────────────────

  const inputStep = el('section', 'redact-step');
  const intro = el(
    'p',
    'hint',
    'Paste a document or open a PDF or text file. It is checked piece by piece by the model ' +
      'chosen above, which runs on this machine; nothing is sent to a remote provider and ' +
      'nothing is saved.',
  );
  const source = el('textarea', 'tool-textarea');
  source.placeholder = 'Paste the text here…';
  source.spellcheck = false;
  const fileInput = el('input');
  fileInput.type = 'file';
  fileInput.accept = '.pdf,.txt,.md,.csv,.eml,.json,.xml,.html,text/*,application/pdf';
  fileInput.hidden = true;
  const openFile = button('Open file…', 'confirm-btn', 'upload');
  openFile.addEventListener('click', () => fileInput.click());
  const sourceInfo = el('span', 'tool-meta');
  const start = button('Redact', 'confirm-btn primary', 'redact');
  const inputError = el('p', 'profile-error');
  const inputBar = el('div', 'tool-bar');
  inputBar.append(openFile, fileInput, sourceInfo, el('span', 'tool-spacer'), start);
  inputStep.append(intro, source, inputBar, inputError);

  // ── step 2: progress ───────────────────────────────────────────────
  //
  // One excerpt on a local model takes anything from two seconds to two
  // minutes, so a bar that has not moved in a minute says nothing about whether
  // anything is happening. What is shown instead: a page being redacted line by
  // line, on a loop that never stops while a request is out, the part being
  // read with a clock that ticks while the model works on it, and a log of
  // every finished part, what it held, what the model proposed that was not in
  // the text, and why a part could not be checked. The reviewer then starts the
  // review already knowing which parts to distrust.

  const progressStep = el('section', 'redact-step redact-progress');
  progressStep.hidden = true;
  const progressHero = el('div', 'redact-hero');
  progressHero.append(iconNode('redacting', 64, 'redact-anim'));
  const progressText = el('p', 'tool-progress-text', 'Preparing…');
  const progressMeta = el('p', 'tool-meta');
  progressHero.append(progressText, progressMeta);
  const current = el('div', 'redact-current');
  current.hidden = true;
  const currentHead = el('p', 'redact-current-head');
  const currentText = el('p', 'redact-current-text');
  const currentSlow = el('p', 'hint');
  currentSlow.hidden = true;
  current.append(currentHead, currentText, currentSlow);
  const progressLog = el('ol', 'redact-log');
  progressLog.setAttribute('aria-live', 'polite');
  const stop = button('Stop', 'confirm-btn');
  const progressBar = el('div', 'tool-bar');
  progressBar.append(el('span', 'tool-spacer'), stop);
  progressStep.append(progressHero, current, progressLog, progressBar);

  /** One finished step in the log, newest last and scrolled into view. */
  function logRow(text, { failed = false, at = '' } = {}) {
    const row = el('li', failed ? 'redact-log-row failed' : 'redact-log-row');
    row.append(iconNode(failed ? 'alert' : 'check', 14), el('span', 'redact-log-text', text));
    if (at) row.append(el('span', 'redact-log-time', at));
    progressLog.append(row);
    row.scrollIntoView?.({ block: 'nearest' });
  }

  // ── step 3: review ─────────────────────────────────────────────────

  const reviewStep = el('section', 'redact-step redact-review');
  reviewStep.hidden = true;

  const notice = el('div', 'redact-notice');
  notice.hidden = true;
  const noticeText = el('p');
  const retry = button('Check again', 'confirm-btn', 'refresh');
  notice.append(iconNode('alert', 16), noticeText, retry);

  const summary = el('p', 'tool-meta redact-summary');
  const undoBtn = iconButton('undo', 'Undo (⌘Z)');
  const redoBtn = iconButton('redo', 'Redo (⇧⌘Z)');
  const tabs = el('div', 'redact-tabs');
  tabs.setAttribute('role', 'tablist');
  const tabReview = button('Review');
  const tabOutput = button('Output');
  for (const tab of [tabReview, tabOutput]) {
    tab.className = 'redact-tab';
    tab.setAttribute('role', 'tab');
  }
  tabs.append(tabReview, tabOutput);
  const toolbar = el('div', 'redact-toolbar');
  toolbar.append(summary, undoBtn, redoBtn, tabs);

  // The document, and the popover that appears over a selection in it.
  const docWrap = el('div', 'redact-docwrap');
  const doc = el('div', 'redact-doc');
  doc.tabIndex = 0;
  const pop = el('div', 'redact-pop');
  pop.hidden = true;
  const popKind = kindSelect('person');
  const popBtn = button('Redact', 'confirm-btn primary', 'redact');
  const popCount = el('span', 'redact-pop-count');
  pop.append(popKind, popBtn, popCount);
  docWrap.append(doc, pop);

  const outputWrap = el('div', 'redact-output');
  outputWrap.hidden = true;
  const editedNote = el('p', 'hint');
  editedNote.hidden = true;
  const rebuild = button('Rebuild from review', 'ghost-btn');
  const output = el('textarea', 'tool-textarea');
  output.spellcheck = false;
  outputWrap.append(editedNote, output);

  // The term panel: find or add at the top, then the three decisions.
  const panel = el('aside', 'redact-panel');
  const findForm = el('form', 'redact-find');
  const findInput = el('input', 'text-input');
  findInput.type = 'search';
  findInput.placeholder = 'Find or add text…';
  findInput.maxLength = 160;
  findInput.setAttribute('aria-label', 'Find a term, or type text to redact');
  findForm.append(findInput);
  // Shown when what was typed is not a term yet: the way to add one by hand
  // that also works on a phone, where selecting text is fiddly.
  const addRow = el('div', 'redact-addrow');
  addRow.hidden = true;
  const addKind = kindSelect('other', 'Kind of the new term');
  const addBtn = button('Redact', 'confirm-btn primary');
  addBtn.type = 'submit';
  const addInfo = el('span', 'redact-addinfo');
  addRow.append(addInfo, addKind, addBtn);
  findForm.append(addRow);

  const lists = el('div', 'redact-lists');
  const checkSection = section('To check', 'rd-suspect');
  const nextBtn = button('Next', 'ghost-btn redact-next', 'chevron');
  nextBtn.title = 'Show the next one in the document (N)';
  checkSection.head.append(nextBtn);
  const redactedSection = section('Redacted', 'rd-on');
  const keptSection = section('Kept', 'rd-off');
  const emptyNote = el('p', 'hint redact-empty');
  lists.append(checkSection.root, redactedSection.root, keptSection.root, emptyNote);
  panel.append(findForm, lists);

  function section(name, swatch) {
    const root = el('section', 'redact-section');
    const head = el('header', 'redact-section-head');
    const label = el('h3');
    label.append(el('mark', `rd ${swatch}`, 'Aa'), el('span', '', name));
    const count = el('span', 'redact-section-count');
    label.append(count);
    head.append(label);
    const list = el('ul', 'redact-terms');
    root.append(head, list);
    return { root, head, count, list };
  }

  const main = el('div', 'redact-main');
  const left = el('div', 'redact-left');
  left.append(docWrap, outputWrap);
  main.append(left, panel);

  const restart = button('Start over', 'confirm-btn');
  const keys = el('span', 'tool-meta redact-keys', 'Select text to redact it · click a mark to toggle it');
  const copy = button('Copy', 'confirm-btn primary', 'copy');
  const reviewBar = el('div', 'tool-bar');
  reviewBar.append(restart, keys, el('span', 'tool-spacer'), copy);

  reviewStep.append(notice, toolbar, main, reviewBar);
  body.append(inputStep, progressStep, reviewStep);

  const show = (step) => {
    for (const s of [inputStep, progressStep, reviewStep]) s.hidden = s !== step;
  };

  // ── model ──────────────────────────────────────────────────────────

  async function loadModels() {
    modelSelect.replaceChildren(el('option', '', 'Looking for local models…'));
    modelSelect.disabled = true;
    const res = await fetch('/api/models?kind=chat')
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
    const local = res?.groups?.local ?? [];
    if (!local.length) {
      const none = el('option', '', 'No local model found');
      modelSelect.replaceChildren(none);
      state.model = null;
      inputError.textContent =
        'Redact runs only on a local model. Start Ollama or LM Studio with a chat model loaded, then reopen this tool.';
      start.disabled = true;
      return;
    }
    modelSelect.replaceChildren(
      ...local.map((m) => {
        const option = el('option', '', m.name);
        option.value = `${m.provider}::${m.id}`;
        return option;
      }),
    );
    modelSelect.disabled = false;
    const saved = settings.values ?? {};
    const wanted = local.find((m) => m.provider === saved.redact_provider && m.id === saved.redact_model) ?? local[0];
    modelSelect.value = `${wanted.provider}::${wanted.id}`;
    state.model = { provider: wanted.provider, id: wanted.id };
    start.disabled = false;
    inputError.textContent = '';
  }

  modelSelect.addEventListener('change', () => {
    const [provider, ...rest] = modelSelect.value.split('::');
    state.model = { provider, id: rest.join('::') };
    settings.patch({ redact_provider: provider, redact_model: state.model.id });
  });

  // ── input ──────────────────────────────────────────────────────────

  const describeSource = () => {
    const n = source.value.length;
    // let locale be decided by the browser, not the server.
    sourceInfo.textContent = n ? `${n.toLocaleString()} characters` : '';
  };
  source.addEventListener('input', describeSource);

  fileInput.addEventListener('change', async () => {
    const file = fileInput.files?.[0];
    fileInput.value = '';
    if (!file) return;
    inputError.textContent = '';
    const isPdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
    if (!isPdf) {
      source.value = await file.text();
      describeSource();
      return;
    }
    sourceInfo.textContent = `Reading ${file.name}…`;
    // A large PDF takes seconds to extract, and a second click meanwhile would
    // race the first for the textarea.
    openFile.disabled = start.disabled = true;
    const res = await post('/api/tools/redact/pdf', file, { raw: true });
    openFile.disabled = false;
    start.disabled = !state.model;
    if (!res.ok) {
      inputError.textContent = res.message;
      describeSource();
      return;
    }
    source.value = res.data.text;
    describeSource();
    sourceInfo.textContent += ` · ${plural(res.data.pages, 'page')} from ${file.name}`;
  });

  // ── scanning ───────────────────────────────────────────────────────

  /** Scan the given excerpts and fold what they hold into the term list.
   *  An excerpt that fails is recorded, not fatal: the rest of the document
   *  is still worth checking, and the review says which parts were not. */
  async function scan(indices) {
    const controller = new AbortController();
    state.running = controller;
    show(progressStep);
    stop.disabled = false;
    stop.lastChild.textContent = 'Stop';
    const found = [];
    const failed = [];
    const durations = [];
    let stopped = false;
    const began = Date.now();
    let partBegan = began;
    let currentPart = 0;

    // Ticks once a second while a part is with the model. With the animation
    // it is the proof that the page has not hung; on its own the animation
    // would only prove that CSS still runs.
    const tick = () => {
      const now = Date.now();
      const left = remaining(durations, indices.length - durations.length);
      progressMeta.textContent =
        `${clock(now - began)} elapsed` +
        (left === null ? '' : ` · about ${clock(left)} left`) +
        ` · ${plural(found.length, 'item')} found so far`;
      currentHead.textContent = `Reading part ${currentPart} · ${clock(now - partBegan)}`;
      // Past half a minute the person deserves to know this is the model being
      // slow, not the page, and what their options are.
      currentSlow.hidden = now - partBegan < 30_000;
    };
    currentSlow.textContent =
      'Still waiting for the model. Large models on a laptop can take a minute or two per part. ' +
      'A part that takes too long gives up on its own and is marked as not checked; ' +
      'Stop keeps everything found so far.';
    const timer = setInterval(tick, 1000);

    try {
      for (const [n, index] of indices.entries()) {
        const [from, to] = state.chunks[index];
        if (stopped) {
          failed.push({ index, start: from, end: to, message: 'stopped before it was checked' });
          continue;
        }
        currentPart = index + 1;
        partBegan = Date.now();
        progressText.textContent =
          indices.length === state.chunks.length
            ? `Checking part ${n + 1} of ${indices.length}`
            : `Checking again: ${n + 1} of ${plural(indices.length, 'part')}`;
        // The start of the excerpt, so the person can see where in their
        // document the model is, not just how far through a count.
        currentText.textContent = `“${state.text.slice(from, Math.min(to, from + 220)).replace(/\s+/g, ' ').trim()}…”`;
        current.hidden = false;
        tick();

        const res = await post(
          '/api/tools/redact/scan',
          { provider: state.model.provider, model: state.model.id, text: state.text.slice(from, to) },
          { signal: controller.signal },
        );
        const took = Date.now() - partBegan;
        if (res.ok) {
          durations.push(took);
          const items = res.data.findings ?? [];
          found.push(...items);
          // Proposed but not in the excerpt: the model guessing. Dropped by
          // the server, and said here so a long run of guesses is noticed.
          const dropped = Math.max(0, (res.data.proposed ?? items.length) - items.length);
          logRow(
            `Part ${index + 1}: ${items.length ? tally(items) : 'nothing found'}` +
              (dropped ? ` · ${dropped} proposed that is not in the text, ignored` : ''),
            { at: clock(took) },
          );
          continue;
        }
        if (!res.aborted) durations.push(took);
        failed.push({ index, start: from, end: to, message: res.message });
        logRow(`Part ${index + 1} not checked: ${res.message}`, { failed: true, at: clock(took) });
        // A refusal (no local model, a bad request) will refuse every excerpt.
        if (res.aborted || res.status === 422 || res.status === 401) stopped = true;
      }
    } finally {
      clearInterval(timer);
      state.running = null;
    }

    const skipped = failed.filter((f) => f.message === 'stopped before it was checked').length;
    if (skipped) logRow(`${plural(skipped, 'part')} left unchecked`, { failed: true });
    current.hidden = true;
    return { found, failed };
  }

  start.addEventListener('click', async () => {
    const text = source.value.replace(/\r\n?/g, '\n');
    if (!text.trim()) {
      inputError.textContent = 'There is nothing to redact yet.';
      return;
    }
    if (!state.model) return;
    inputError.textContent = '';
    start.disabled = true;
    progressLog.replaceChildren();
    progressMeta.textContent = '';
    progressText.textContent = 'Preparing…';
    show(progressStep);
    const plan = await post('/api/tools/redact/plan', { text });
    start.disabled = false;
    if (!plan.ok) {
      show(inputStep);
      inputError.textContent = plan.message;
      return;
    }
    state.text = text;
    state.chunks = plan.data.chunks;
    state.outputEdited = false;
    state.undo = [];
    state.redo = [];
    state.filter = findInput.value = '';
    // The patterns run first and take no time, so the log opens with what is
    // already certain before the model has read a word.
    const hits = detect(text);
    const sure = hits.filter((h) => h.sure);
    const unsure = hits.length - sure.length;
    logRow(
      `Patterns: ${sure.length ? tally(sure) : 'nothing certain'}` +
        (unsure ? ` · ${plural(unsure, 'possible item')} to check in the review` : ''),
    );
    logRow(`Split into ${plural(state.chunks.length, 'part')} for ${state.model.id}`);
    const { found, failed } = await scan(state.chunks.map((_, i) => i));
    state.failed = failed;
    state.terms = mergeTerms(found, hits);
    showReview();
  });

  stop.addEventListener('click', () => {
    if (!state.running) return;
    stop.disabled = true;
    stop.lastChild.textContent = 'Stopping…';
    state.running.abort();
  });

  retry.addEventListener('click', async () => {
    progressLog.replaceChildren();
    const { found, failed } = await scan(state.failed.map((f) => f.index));
    state.failed = failed;
    commit(() => {
      state.terms = mergeTerms(found, [], state.terms);
    });
    showReview();
  });

  // ── editing ────────────────────────────────────────────────────────

  /** Every change to the terms goes through here, so each one can be undone. */
  function commit(change) {
    state.undo.push(structuredClone(state.terms));
    if (state.undo.length > 200) state.undo.shift();
    state.redo = [];
    change();
    render();
  }

  function travel(from, to) {
    if (!from.length) return;
    to.push(structuredClone(state.terms));
    state.terms = from.pop();
    render();
  }
  const undo = () => travel(state.undo, state.redo);
  const redo = () => travel(state.redo, state.undo);
  undoBtn.addEventListener('click', undo);
  redoBtn.addEventListener('click', redo);

  const termOf = (key) => state.terms.find((t) => t.key === key);

  function setOn(key, on) {
    commit(() => {
      const term = termOf(key);
      if (term) Object.assign(term, { on, decided: true });
    });
  }

  function setKind(key, kind) {
    commit(() => {
      const term = termOf(key);
      if (term) Object.assign(term, { kind, decided: true });
    });
  }

  /** Add a hand-picked string, or switch on the term it already is. */
  function addTerm(text, kind) {
    const value = text.replace(/\s+/g, ' ').trim();
    if (!value || !findAll(state.text, value).length) return false;
    commit(() => {
      const key = keyOf(value);
      const known = termOf(key);
      if (known) Object.assign(known, { on: true, kind, decided: true });
      else state.terms.push({ key, text: value, kind, on: true, origin: 'manual', decided: true });
    });
    return true;
  }

  // ── review ─────────────────────────────────────────────────────────

  let spans = [];
  let last = null; // the panel's input from the latest render, for filtering
  // Which occurrence of each term was shown last, so "show" walks through them.
  const shownAt = new Map();

  function showReview() {
    show(reviewStep);
    selectTab('review');
    render();
  }

  /** The gaps between marks, split again where an excerpt went unchecked, so
   *  the parts nobody read are tinted rather than looking like clean text. */
  function appendGap(parent, from, to) {
    let at = from;
    for (const f of state.failed) {
      if (f.end <= at || f.start >= to) continue;
      if (f.start > at) parent.append(document.createTextNode(state.text.slice(at, f.start)));
      const cut = Math.min(f.end, to);
      parent.append(el('span', 'rd-unchecked', state.text.slice(Math.max(f.start, at), cut)));
      at = cut;
    }
    if (at < to) parent.append(document.createTextNode(state.text.slice(at, to)));
  }

  function render() {
    spans = layout(state.text, state.terms);
    const names = labels(spans, state.terms);
    const byKey = new Map(state.terms.map((t) => [t.key, t]));

    // Rebuilding the document resets nothing the reader can see as long as
    // the scroll position is put back: the text is the same length, only the
    // marks around it change.
    const scrollTop = doc.scrollTop;
    const nodes = document.createDocumentFragment();
    let at = 0;
    for (const span of spans) {
      const term = byKey.get(span.key);
      appendGap(nodes, at, span.start);
      const mark = el('mark', 'rd', state.text.slice(span.start, span.end));
      mark.dataset.key = span.key;
      if (term.on) {
        mark.classList.add('rd-on');
        mark.dataset.label = names.get(span.key) ?? '';
        mark.title = `${term.kind} · click to keep`;
      } else if (term.origin === 'suspect' && !term.decided) {
        mark.classList.add('rd-suspect');
        mark.title = `possibly ${term.kind} · click to redact`;
      } else {
        mark.classList.add('rd-off');
        mark.title = `${term.kind} · kept · click to redact`;
      }
      nodes.append(mark);
      at = span.end;
    }
    appendGap(nodes, at, state.text.length);
    doc.replaceChildren(nodes);
    doc.scrollTop = scrollTop;

    const groups = sections(state.terms, spans);
    last = { groups, names };
    renderPanel(groups, names);

    summary.textContent =
      `${plural(groups.redacted.length, 'term')} redacted` +
      (groups.check.length ? ` · ${groups.check.length} to check` : '') +
      ` · ${plural(state.chunks.length, 'part')} read by ${state.model?.id ?? 'the model'}`;

    if (state.failed.length) {
      const reasons = [...new Set(state.failed.map((f) => f.message))].join('; ');
      noticeText.textContent =
        `${plural(state.failed.length, 'part')} of ${state.chunks.length} could not be checked by the model ` +
        `(${reasons}). Only the patterns ran there. The tinted text was not read by the model; read it yourself.`;
      notice.hidden = false;
    } else {
      notice.hidden = true;
    }

    undoBtn.disabled = !state.undo.length;
    redoBtn.disabled = !state.redo.length;
    if (!state.outputEdited) output.value = redactText(state.text, spans, state.terms, names);
    editedNote.hidden = !state.outputEdited;
  }

  // ── panel ──────────────────────────────────────────────────────────

  function renderPanel(groups, names) {
    const needle = keyOf(state.filter);
    const match = (t) => !needle || t.key.includes(needle);
    const fill = (part, terms, row) => {
      const shown = terms.filter(match);
      part.count.textContent = terms.length ? String(terms.length) : '';
      part.list.replaceChildren(...shown.map(row));
      part.root.hidden = !shown.length;
    };
    fill(checkSection, groups.check, (t) => termRow(t, groups.counts, 'check'));
    fill(redactedSection, groups.redacted, (t) => termRow(t, groups.counts, 'on', names.get(t.key)));
    fill(keptSection, groups.kept, (t) => termRow(t, groups.counts, 'off'));
    nextBtn.hidden = !groups.check.length;

    const visible = [checkSection, redactedSection, keptSection].some((s) => !s.root.hidden);
    emptyNote.hidden = visible;
    emptyNote.textContent = needle
      ? 'No term matches. Add it below the search field if it should be redacted.'
      : 'Nothing found yet. Select text in the document, or type it above, to redact it.';
    describeAdd();
  }

  function termRow(term, counts, mode, label = '') {
    const row = el('li', `redact-term ${mode}`);
    if (mode !== 'check') {
      const toggle = el('input');
      toggle.type = 'checkbox';
      toggle.checked = term.on;
      toggle.title = term.on ? 'Redacted · untick to keep' : 'Kept · tick to redact';
      toggle.setAttribute('aria-label', `Redact ${term.text}`);
      toggle.addEventListener('change', () => setOn(term.key, toggle.checked));
      row.append(toggle);
    }
    const n = counts.get(term.key) ?? 0;
    const name = el('button', 'redact-term-text', term.text);
    name.type = 'button';
    name.title = n ? `Show in the document (${plural(n, 'time')})` : 'Not in the text on its own';
    name.disabled = !n;
    name.addEventListener('click', () => reveal(term.key));
    // The placeholder is in the document beside every mark; here it only
    // takes the room the name needs, so it rides along as the tooltip.
    if (label) name.title = `${label} · ${name.title}`;
    row.append(name);
    if (n > 1) row.append(el('span', 'redact-term-count', `×${n}`));
    const kind = kindSelect(term.kind, `Kind of ${term.text}`);
    kind.addEventListener('change', () => setKind(term.key, kind.value));
    row.append(kind);
    if (mode === 'check') {
      const yes = iconButton('check', `Redact ${term.text}`);
      yes.classList.add('redact-yes');
      yes.addEventListener('click', () => setOn(term.key, true));
      const no = iconButton('close', `Keep ${term.text}`);
      no.addEventListener('click', () => setOn(term.key, false));
      row.append(yes, no);
    }
    return row;
  }

  /** Scroll to the next occurrence of a term and make it blink once. */
  function reveal(key) {
    selectTab('review');
    const marks = [...doc.querySelectorAll('mark.rd')].filter((m) => m.dataset.key === key);
    if (!marks.length) return;
    const index = ((shownAt.get(key) ?? -1) + 1) % marks.length;
    shownAt.set(key, index);
    flash(marks[index]);
  }

  function flash(mark) {
    mark.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
    mark.classList.remove('rd-flash');
    void mark.offsetWidth; // restart the animation on a second click
    mark.classList.add('rd-flash');
  }

  /** The next suspect below the one shown last, wrapping at the end. */
  let lastSuspect = -1;
  function nextSuspect() {
    selectTab('review');
    const marks = [...doc.querySelectorAll('mark.rd-suspect')];
    if (!marks.length) return;
    lastSuspect = (lastSuspect + 1) % marks.length;
    flash(marks[lastSuspect]);
  }
  nextBtn.addEventListener('click', nextSuspect);

  // Find filters the lists; text that is not a term yet can be added from here.
  function describeAdd() {
    const value = state.filter.trim();
    const known = value && termOf(keyOf(value));
    if (!value || known) {
      addRow.hidden = true;
      return;
    }
    const n = findAll(state.text, value).length;
    addRow.hidden = false;
    addBtn.disabled = !n;
    addInfo.textContent = n ? `“${value}” occurs ${plural(n, 'time')}` : 'Not in the text as a whole word';
  }

  findInput.addEventListener('input', () => {
    const before = state.filter.trim();
    state.filter = findInput.value;
    // Only guess when the text changed from nothing, so a kind the person
    // picked is not overwritten on every keystroke.
    if (!before) addKind.value = guessKind(state.filter);
    // The panel only: the document does not change with a search.
    if (last) renderPanel(last.groups, last.names);
  });
  findForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (addRow.hidden || addBtn.disabled) return;
    const value = state.filter;
    state.filter = findInput.value = '';
    addTerm(value, addKind.value);
  });

  // ── the document ───────────────────────────────────────────────────

  doc.addEventListener('click', (event) => {
    const mark = event.target.closest?.('mark.rd');
    if (!mark) return;
    // A click that ended a selection is a selection, not a toggle.
    if (String(window.getSelection?.() ?? '').trim()) return;
    const term = termOf(mark.dataset.key);
    if (term) setOn(term.key, !term.on);
  });

  doc.addEventListener('keydown', (event) => {
    if (event.key === 'n' && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      nextSuspect();
    }
  });

  // A selection in the document gets a small toolbar right above it: what the
  // old add-form at the top asked for in three steps, select, scroll up, click.
  let selected = '';
  function placePop() {
    const selection = window.getSelection?.();
    // Choosing a kind moves focus into the popover; that must not close it.
    if (pop.contains(document.activeElement)) return;
    if (!selection || selection.isCollapsed || !doc.contains(selection.anchorNode)) {
      pop.hidden = true;
      return;
    }
    const text = selection.toString().replace(/\s+/g, ' ').trim();
    if (!text || text.length > 160) {
      pop.hidden = true;
      return;
    }
    selected = text;
    const n = findAll(state.text, text).length;
    popKind.value = termOf(keyOf(text))?.kind ?? guessKind(text);
    popBtn.disabled = !n;
    popCount.textContent = n ? (n > 1 ? `×${n}` : '') : 'not a whole word';
    pop.hidden = false;

    const range = selection.getRangeAt(0).getBoundingClientRect();
    const box = docWrap.getBoundingClientRect();
    const width = pop.offsetWidth;
    const height = pop.offsetHeight;
    let top = range.top - box.top - height - 8;
    if (top < 4) top = range.bottom - box.top + 8; // no room above: below it
    const left = Math.min(Math.max(4, range.left - box.left + range.width / 2 - width / 2), box.width - width - 4);
    pop.style.top = `${Math.round(top)}px`;
    pop.style.left = `${Math.round(left)}px`;
  }
  let popFrame = 0;
  const onSelection = () => {
    cancelAnimationFrame(popFrame);
    popFrame = requestAnimationFrame(placePop);
  };
  document.addEventListener('selectionchange', onSelection);
  doc.addEventListener('scroll', () => {
    if (!pop.hidden) placePop();
  });
  // Pressing the button must not collapse the selection before it is read.
  popBtn.addEventListener('pointerdown', (event) => event.preventDefault());
  popBtn.addEventListener('click', () => {
    if (addTerm(selected, popKind.value)) {
      window.getSelection?.()?.removeAllRanges?.();
      pop.hidden = true;
    }
  });
  popKind.addEventListener('change', () => popBtn.focus());
  pop.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      pop.hidden = true;
      doc.focus();
    }
  });
  pop.addEventListener('focusout', (event) => {
    if (!pop.contains(event.relatedTarget)) onSelection();
  });

  // ── output ─────────────────────────────────────────────────────────

  function selectTab(which) {
    const review = which === 'review';
    tabReview.setAttribute('aria-selected', String(review));
    tabOutput.setAttribute('aria-selected', String(!review));
    docWrap.hidden = !review;
    outputWrap.hidden = review;
    if (!review) pop.hidden = true;
  }
  tabReview.addEventListener('click', () => selectTab('review'));
  tabOutput.addEventListener('click', () => selectTab('output'));

  // Editing the output by hand is the last word: a later click in the review
  // must not silently throw the edit away, so it stops following the review
  // until it is rebuilt on purpose.
  output.addEventListener('input', () => {
    state.outputEdited = true;
    editedNote.hidden = false;
  });
  editedNote.append(
    document.createTextNode('Edited by hand, so changes in Review no longer reach it. '),
    rebuild,
  );
  rebuild.addEventListener('click', () => {
    state.outputEdited = false;
    render();
  });

  copy.addEventListener('click', async () => {
    const text = output.value;
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      // Clipboard API refused (no secure context, or no permission): fall back
      // to the selection the browser has always been able to copy.
      selectTab('output');
      output.select();
      ok = document.execCommand?.('copy') ?? false;
    }
    copy.lastChild.textContent = ok ? 'Copied' : 'Select and copy by hand';
    setTimeout(() => (copy.lastChild.textContent = 'Copy'), 1600);
  });

  restart.addEventListener('click', () => {
    show(inputStep);
    source.focus();
  });

  // Undo and redo for the terms, anywhere in the review except inside a field,
  // which keeps its own.
  dialog.addEventListener('keydown', (event) => {
    if (reviewStep.hidden || isField(event.target)) return;
    if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'z') return;
    event.preventDefault();
    if (event.shiftKey) redo();
    else undo();
  });

  // ── closing ────────────────────────────────────────────────────────

  const hasWork = () => state.running || !reviewStep.hidden;

  async function requestClose() {
    if (
      hasWork() &&
      !(await confirmDialog({
        title: 'Close Redact?',
        body: 'Nothing here is saved. The document and the review are gone once this closes.',
        confirm: 'Close',
      }))
    ) {
      return;
    }
    dialog.close();
  }

  // Escape lands here as `cancel`. Swallowed while there is work to lose, so a
  // stray keypress after a ten-minute scan cannot throw it away.
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    if (!pop.hidden) {
      pop.hidden = true;
      return;
    }
    requestClose();
  });
  close.addEventListener('click', requestClose);
  dialog.addEventListener('close', () => {
    state.running?.abort();
    document.removeEventListener('selectionchange', onSelection);
    dialog.remove();
  });

  document.body.append(dialog);
  dialog.showModal();
  source.focus();
  loadModels();
  return dialog;
}
