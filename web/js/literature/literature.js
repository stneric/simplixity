// The literature page: talk through what you need, search the paper
// databases in rounds, screen what comes back, export BibTeX.
//
// The server does the work (app/tools/literature.py) and keeps everything; this
// module is the screening desk. Three things it is careful about:
//
// * **Screening is driven from here, a few papers at a time.** Summaries are
//   model calls, and twenty-five of them in one request would be a minute of
//   nothing on screen followed by everything at once. The loop below asks for
//   the papers on screen first, shows each summary as it lands, stops when the
//   reading list changes, and leaves a failed batch marked so it can be retried.
// * **A decision is one keystroke.** 1, 2, 3 for relevant, maybe, out; j and k
//   to move; u to undo. A card that leaves the current view hands focus to the
//   next one, so a page of abstracts can be screened without the mouse.
// * **Every failure is said where it happened.** A database that rate-limited
//   is a line under the round that searched it, a summary that could not be
//   written is a line on its card. Nothing is dropped quietly.
//
// Like the rest of the frontend, nothing here assigns innerHTML: titles and
// abstracts come from third-party databases and go in through textContent.

import { hydrate, iconNode } from '../shared/icons.js';
import { ensureProfile } from '../shared/profile.js';
import { confirmDialog, promptDialog } from '../shared/dialog.js';
import { outbound } from '../render/links.js';
import { ModelPicker } from '../shared/picker.js';
import * as core from './literature-core.js';

hydrate();

const BATCH = 6; // the server's SCREEN_BATCH
const GREETING =
  'Tell me what you need sources for: the topic, what you will use them for (a thesis ' +
  'chapter, a related-work section, a review), and anything that is in or out of scope. ' +
  'I will ask what I need to know, then search.';

const $ = (id) => document.getElementById(id);

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
  if (label) node.append(el('span', '', label));
  return node;
}

const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A small turning ring, the one sign of work on this page. */
function spinner() {
  const node = el('span', 'lit-spinner');
  node.setAttribute('aria-hidden', 'true');
  return node;
}

/**
 * Run `task` with `control` saying that it is working.
 *
 * Every button here starts a request that takes from a moment to half a
 * minute (an upload is read, matched, looked up and embedded in one), and a
 * button that does nothing visible for that long gets clicked again. Its icon
 * turns into the spinner, it is disabled, and the row it acts on (`row`) is
 * dimmed, until the task settles either way. A second click while busy is
 * ignored rather than queued.
 */
async function busy(control, task, { row = null } = {}) {
  if (control.getAttribute('aria-busy') === 'true') return undefined;
  const icon = control.querySelector(':scope > .icon-slot');
  const spin = spinner();
  if (icon) icon.replaceWith(spin);
  else control.prepend(spin);
  control.setAttribute('aria-busy', 'true');
  control.disabled = true;
  row?.classList.add('is-busy');
  try {
    return await task();
  } finally {
    if (icon && spin.isConnected) spin.replaceWith(icon);
    else spin.remove();
    control.removeAttribute('aria-busy');
    control.disabled = false;
    row?.classList.remove('is-busy');
  }
}

/**
 * Mark the nodes this view has not shown before, so they ease in.
 *
 * The lists here are rebuilt whole on every change, a decision, a summary
 * landing, a tab, and an entrance on each rebuild would set the whole page
 * shimmering every time a key is pressed. Only what is new to the reader
 * moves: a fresh round's papers, a tab just opened, an upload's row. A short
 * stagger, capped, so a list of forty does not take two seconds to arrive.
 */
function enter(items, keyOf) {
  items.forEach(({ node, item }, i) => {
    const key = keyOf(item);
    if (seen.has(key)) return;
    seen.add(key);
    node.classList.add('lit-enter');
    node.style.animationDelay = `${Math.min(i, 8) * 30}ms`;
  });
}
const seen = new Set();

/** JSON in, `{ ok, status, data, message }` out, never a throw. */
async function api(method, url, body, { signal } = {}) {
  try {
    const res = await fetch(url, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    if (res.status === 401) {
      // The session ended (a password changed elsewhere). The chat page owns
      // the picker flow; this page simply starts over.
      location.reload();
      return { ok: false, status: 401, data: {}, message: 'Choose a profile first' };
    }
    const data = await res.json().catch(() => ({}));
    const detail = typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`;
    return { ok: res.ok, status: res.status, data, message: res.ok ? '' : detail };
  } catch (error) {
    if (error?.name === 'AbortError') return { ok: false, status: 0, aborted: true, data: {}, message: '' };
    return { ok: false, status: 0, data: {}, message: 'The server is not answering.' };
  }
}

const isField = (node) => node?.closest?.('input, textarea, select, [contenteditable="true"]');

// ── state ────────────────────────────────────────────────────────────

const state = {
  lists: [],
  sources: [],
  ieee: false,
  projects: [],
  current: null, // the open collection
  papers: [],
  files: [], // uploaded full texts, assigned to a paper or waiting
  uploading: null, // { done, total, name } while uploads run
  uploadNotes: [], // what happened to each upload that needs saying
  candidates: {}, // file id → paper ids it might be, from the upload's match
  indexing: false,
  filter: 'open',
  focusId: null,
  openAbstracts: new Set(),
  loading: new Set(), // paper ids with a summary request out
  failed: new Set(), // paper ids whose summary could not be written
  screening: null, // AbortController while the loop runs
  screenError: '',
  last: null, // { id, before } for undo
  talking: false,
  searching: false,
  settings: null, // the profile's saved settings, for the model choice
};

const main = $('lit-main');
const nodes = {}; // the parts of the open collection's view that re-render

// ── model ──────────────────────────────────────────────────────────
//
// One picker for the page, built once and moved into each collection's top bar:
// the picker listens on the document, so building one per render would leave
// a listener behind for every list ever opened. The choice is a profile
// setting, `literature_model`, shared by every reading list; empty means the
// chat model, which is what the picker then shows.

const modelSlot = el('div', 'lit-model');
const modelNote = el('span', 'lit-model-note');
const picker = new ModelPicker(modelSlot, 'chat', saveModel);
modelSlot.title =
  'Talks, plans the searches and screens the abstracts, for every reading list, and ' +
  'writes and checks the answers in projects with a reading list. A small model does all ' +
  'of it noticeably worse.';

function syncModel() {
  const s = state.settings ?? {};
  const own = Boolean(s.literature_model);
  picker.setSelection(
    own ? s.literature_provider || s.chat_provider : s.chat_provider,
    own ? s.literature_model : s.chat_model,
  );
  modelNote.textContent = own ? '' : 'same as chat';
}

async function loadSettings() {
  const res = await api('GET', '/api/settings');
  if (!res.ok) return;
  state.settings = res.data;
  syncModel();
  picker.load();
}

async function saveModel(selection) {
  const next = {
    ...state.settings,
    literature_provider: selection.provider,
    literature_model: selection.id,
  };
  const res = await api('PUT', '/api/settings', next);
  if (res.ok) state.settings = res.data;
  syncModel();
}

// ── sidebar ──────────────────────────────────────────────────────────

async function loadLists() {
  const res = await api('GET', '/api/literature');
  if (!res.ok) return;
  state.lists = res.data.collections ?? [];
  state.sources = res.data.sources ?? [];
  state.ieee = Boolean(res.data.ieee);
  renderLists();
  const names = state.sources.map((s) => s.label).join(', ');
  $('lit-sources').textContent =
    `Searches ${names}.` + (state.ieee ? '' : ' IEEE Xplore needs IEEE_API_KEY in the server’s environment.');
}

function renderLists() {
  const nav = $('lit-lists');
  nav.replaceChildren(
    ...state.lists.map((list) => {
      const row = el('a', 'lit-list');
      row.href = `#list=${list.id}`;
      if (list.id === state.current?.id) row.setAttribute('aria-current', 'page');
      const counts = [];
      if (list.relevant) counts.push(`${list.relevant} relevant`);
      if (list.open) counts.push(`${list.open} to screen`);
      row.append(el('span', 'lit-list-name', list.name), el('span', 'lit-list-meta', counts.join(' · ') || 'empty'));
      return row;
    }),
  );
}

async function loadProjects() {
  const res = await api('GET', '/api/projects');
  state.projects = res.ok ? res.data.projects ?? [] : [];
}

// ── opening a collection ─────────────────────────────────────────────

async function open(id) {
  stopScreening();
  const res = await api('GET', `/api/literature/${id}`);
  if (!res.ok) {
    state.current = null;
    renderEmpty(res.status === 404 ? 'That reading list does not exist, or belongs to another profile.' : res.message);
    return;
  }
  state.current = res.data.collection;
  state.papers = res.data.papers ?? [];
  state.files = res.data.files ?? [];
  state.uploadNotes = [];
  state.candidates = {};
  state.failed.clear();
  state.loading.clear();
  state.openAbstracts.clear();
  state.screenError = '';
  state.last = null;
  state.filter = core.tally(state.papers).open ? 'open' : state.papers.length ? 'all' : 'open';
  state.focusId = null;
  seen.clear();
  renderCollection();
  renderLists();
  screenLoop();
}

async function createList() {
  const res = await api('POST', '/api/literature', { name: '' });
  if (!res.ok) return;
  await loadLists();
  location.hash = `list=${res.data.id}`;
}

function route() {
  const id = new URLSearchParams(location.hash.slice(1)).get('list');
  if (id) open(id);
  else {
    stopScreening();
    state.current = null;
    renderEmpty();
    renderLists();
  }
}

// ── empty state ──────────────────────────────────────────────────────

function renderEmpty(error = '') {
  const box = el('section', 'lit-empty');
  box.append(
    el('h2', 'lit-empty-title', 'Build a reading list'),
    el(
      'p',
      'lit-empty-lede',
      'Describe what you need the literature for. Simplixity asks a few questions, searches the ' +
        'paper databases, and summarises each abstract against your criteria so you can screen ' +
        'quickly. Every round learns from what you marked relevant and what you excluded. Export ' +
        'the result as BibTeX.',
    ),
  );
  const steps = el('ol', 'lit-steps');
  for (const text of [
    'Talk: what the sources are for, what is in and out',
    'Search: IEEE Xplore, OpenAlex, Semantic Scholar, Crossref, arXiv',
    'Screen: 1 relevant · 2 maybe · 3 exclude',
    'Search again: recommendations and the citation graph around your picks',
    'Export .bib: every entry has the link to download the paper',
    'Add the PDFs under Full texts, file the list under a project, and ask that project’s chat',
  ]) {
    steps.append(el('li', '', text));
  }
  const start = button('New reading list', 'confirm-btn primary', 'plus');
  start.addEventListener('click', createList);
  box.append(steps, start);
  if (error) box.append(el('p', 'profile-error', error));
  main.replaceChildren(box);
}

// ── the collection view ──────────────────────────────────────────────

function renderCollection() {
  const c = state.current;

  // Top bar: name, project, export, delete.
  const top = el('header', 'lit-top');
  const name = el('button', 'lit-name', c.name);
  name.type = 'button';
  name.title = 'Rename';
  name.addEventListener('click', rename);
  nodes.name = name;

  const project = el('select', 'lit-project');
  project.setAttribute('aria-label', 'Project');
  const none = el('option', '', 'No project');
  none.value = '';
  project.append(none);
  for (const p of state.projects) {
    const option = el('option', '', p.name);
    option.value = p.id;
    project.append(option);
  }
  project.value = c.project_id ?? '';
  project.title =
    'Chats in this project answer from the papers marked relevant here, and from their full texts.';
  // A pill like the composer's chips, with the select laid over it: the
  // native control keeps the keyboard and the platform's own list, and the
  // pill keeps the page from looking like a form.
  const projectPill = el('label', 'lit-pill lit-select-pill');
  projectPill.append(iconNode('folder', 14), project, iconNode('chevron', 12, 'icon-slot lit-chevron'));
  project.addEventListener('change', async () => {
    const res = await busy(projectPill, () =>
      api('PATCH', `/api/literature/${c.id}`, { project_id: project.value }),
    );
    if (res?.ok) c.project_id = project.value || null;
    else project.value = c.project_id ?? '';
    if (state.filter === 'fulltext') renderPapers();
  });

  // A toggle chip rather than a checkbox, as the composer's Web and Chats are.
  const withMaybe = button('with maybes', 'lit-pill lit-toggle');
  withMaybe.setAttribute('aria-pressed', 'false');
  withMaybe.title = 'Export the papers marked maybe as well';
  withMaybe.addEventListener('click', () => {
    withMaybe.setAttribute('aria-pressed', String(withMaybe.getAttribute('aria-pressed') !== 'true'));
  });
  const exportBtn = button('Export .bib', 'lit-pill', 'download');
  exportBtn.addEventListener('click', () =>
    busy(exportBtn, () => exportBib(withMaybe.getAttribute('aria-pressed') === 'true')),
  );
  const remove = el('button', 'icon-btn');
  remove.type = 'button';
  remove.title = 'Delete reading list';
  remove.setAttribute('aria-label', 'Delete reading list');
  remove.append(iconNode('trash', 16));
  remove.addEventListener('click', deleteList);
  const model = el('div', 'lit-model-wrap');
  model.append(modelSlot, modelNote);
  top.append(name, projectPill, model, el('span', 'tool-spacer'), withMaybe, exportBtn, remove);

  // Left: the conversation, the brief, the search, the round log.
  const left = el('section', 'lit-col lit-left');
  nodes.thread = el('div', 'lit-thread');
  nodes.thread.setAttribute('aria-live', 'polite');
  // The chat page's composer, in small: one well with the text and a round
  // send button inside it, growing with what is typed.
  const form = el('form', 'lit-composer');
  const input = el('textarea', 'lit-input');
  input.rows = 2;
  input.placeholder = 'What do you need sources for?';
  input.setAttribute('aria-label', 'Message');
  const send = el('button', 'send lit-send');
  send.type = 'submit';
  send.dataset.state = 'send';
  send.setAttribute('aria-label', 'Send');
  send.append(iconNode('send', 16));
  nodes.input = input;
  nodes.send = send;
  nodes.talkError = el('p', 'profile-error');
  const bar = el('div', 'lit-composer-bar');
  bar.append(el('span', 'lit-composer-hint', 'Enter to send · Shift+Enter for a new line'), send);
  form.append(input, bar);
  const grow = () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
  };
  input.addEventListener('input', grow);
  nodes.grow = grow;
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    sendMessage();
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });

  nodes.brief = el('details', 'lit-brief');
  nodes.search = el('div', 'lit-search');
  nodes.rounds = el('div', 'lit-rounds');
  left.append(nodes.thread, form, nodes.talkError, nodes.brief, nodes.search, nodes.rounds);

  // Right: the screening desk.
  const right = el('section', 'lit-col lit-right');
  nodes.tabs = el('div', 'lit-tabs');
  nodes.tabs.setAttribute('role', 'tablist');
  nodes.progress = el('div', 'lit-progress');
  nodes.undo = el('div', 'lit-undo');
  nodes.list = el('div', 'lit-papers');
  right.append(nodes.tabs, nodes.progress, nodes.undo, nodes.list);

  const body = el('div', 'lit-body');
  body.append(left, right);
  main.replaceChildren(top, body);
  bindDrop(right);

  nodes.turns = undefined;
  renderThread();
  renderBrief();
  renderSearch();
  renderRounds();
  renderPapers();
  if (!c.talk.length) input.focus();
}

async function rename() {
  const c = state.current;
  const name = await promptDialog({ title: 'Rename reading list', value: c.name, confirm: 'Rename' });
  if (!name) return;
  const res = await api('PATCH', `/api/literature/${c.id}`, { name });
  if (!res.ok) return;
  c.name = name;
  nodes.name.textContent = name;
  loadLists();
}

async function deleteList() {
  const c = state.current;
  const ok = await confirmDialog({
    title: `Delete “${c.name}”?`,
    body: 'Every paper in it, your decisions and notes go with it. Export the .bib first if you need it.',
  });
  if (!ok) return;
  const res = await api('DELETE', `/api/literature/${c.id}`);
  if (!res.ok) return;
  stopScreening();
  state.current = null;
  await loadLists();
  location.hash = '';
}

// ── conversation ─────────────────────────────────────────────────────

function renderThread() {
  const turns = state.current.talk.length
    ? state.current.talk
    : [{ role: 'assistant', content: GREETING }];
  const shown = nodes.turns ?? turns.length;
  nodes.thread.replaceChildren(
    ...turns.map((turn, i) => {
      const node = el('div', `lit-turn ${turn.role === 'user' ? 'user' : 'assistant'}`, turn.content);
      if (i >= shown) node.classList.add('lit-enter');
      return node;
    }),
  );
  nodes.turns = turns.length;
  if (state.talking) {
    // Three dots rather than a word: the reply's own place in the thread,
    // holding it until the words arrive.
    const typing = el('div', 'lit-turn assistant lit-typing lit-enter');
    typing.setAttribute('aria-label', 'Thinking');
    typing.append(el('span'), el('span'), el('span'));
    nodes.thread.append(typing);
  }
  nodes.thread.lastElementChild?.scrollIntoView({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
}

async function sendMessage() {
  const text = nodes.input.value.trim();
  if (!text || state.talking) return;
  const c = state.current;
  state.talking = true;
  nodes.talkError.textContent = '';
  // Shown at once; stored only with the reply, which is the server's rule too.
  c.talk = [...c.talk, { role: 'user', content: text }];
  nodes.input.value = '';
  nodes.grow();
  renderThread();

  const res = await busy(nodes.send, () => api('POST', `/api/literature/${c.id}/talk`, { message: text }));
  state.talking = false;
  if (state.current !== c) return;
  if (!res.ok) {
    c.talk = c.talk.slice(0, -1);
    nodes.input.value = text; // kept, so sending again is one keystroke
    nodes.grow();
    nodes.talkError.textContent = res.message;
    renderThread();
    return;
  }
  c.talk = res.data.talk;
  c.brief = res.data.brief;
  if (res.data.name && res.data.name !== c.name) {
    c.name = res.data.name;
    nodes.name.textContent = c.name;
    loadLists();
  }
  renderThread();
  renderBrief({ open: true });
  renderSearch({ ready: res.data.ready });
  nodes.input.focus();
}

// ── brief ────────────────────────────────────────────────────────────

let briefTimer = null;

function renderBrief({ open = false } = {}) {
  const brief = state.current.brief ?? {};
  const wasOpen = nodes.brief.open;
  const summary = el('summary', 'lit-brief-head');
  summary.append(el('span', '', 'Brief'), el('span', 'lit-brief-q', brief.question || 'not set yet'));

  const form = el('form', 'lit-brief-form');
  const field = (label, name, value, { multiline = false, hint = '', type = 'text' } = {}) => {
    const wrap = el('label', 'lit-field');
    wrap.append(el('span', 'lit-field-label', label));
    const input = el(multiline ? 'textarea' : 'input', multiline ? 'tool-textarea' : 'text-input');
    if (!multiline) input.type = type;
    else input.rows = Math.max(2, String(value).split('\n').length);
    input.name = name;
    input.value = value ?? '';
    wrap.append(input);
    if (hint) wrap.append(el('span', 'hint', hint));
    return wrap;
  };
  const years = el('div', 'lit-years');
  years.append(
    field('From year', 'year_from', brief.year_from || '', { type: 'number' }),
    field('To year', 'year_to', brief.year_to || '', { type: 'number' }),
  );
  form.append(
    field('Question', 'question', brief.question, { multiline: true }),
    field('Purpose', 'purpose', brief.purpose),
    field('Field', 'field', brief.field),
    field('Include', 'include', (brief.include ?? []).join('\n'), { multiline: true, hint: 'One criterion per line.' }),
    field('Exclude', 'exclude', (brief.exclude ?? []).join('\n'), { multiline: true, hint: 'One criterion per line.' }),
    years,
    field('Search queries', 'queries', (brief.queries ?? []).join('\n'), {
      multiline: true,
      hint: 'English, one per line. The first round runs these; later rounds write their own.',
    }),
  );
  const language = el('input');
  language.type = 'hidden';
  language.name = 'language';
  language.value = brief.language ?? '';
  form.append(language);
  form.addEventListener('input', () => {
    clearTimeout(briefTimer);
    briefTimer = setTimeout(() => saveBrief(form), 600);
  });
  form.addEventListener('submit', (event) => event.preventDefault());

  nodes.brief.replaceChildren(summary, form);
  nodes.brief.open = open || wasOpen;
}

async function saveBrief(form) {
  const c = state.current;
  const values = Object.fromEntries(new FormData(form).entries());
  const brief = core.briefFromForm(values);
  const res = await api('PATCH', `/api/literature/${c.id}`, { brief });
  if (!res.ok || state.current !== c) return;
  c.brief = res.data.collection.brief;
  nodes.brief.querySelector('.lit-brief-q').textContent = c.brief.question || 'not set yet';
  renderSearch();
}

// ── search ───────────────────────────────────────────────────────────

function renderSearch({ ready = false } = {}) {
  const c = state.current;
  const counts = core.tally(state.papers);
  const first = !c.rounds.length;
  const go = button(
    state.searching ? 'Searching…' : first ? 'Search' : 'Search again',
    `lit-go${ready ? ' ready' : ''}`,
    state.searching ? '' : 'search',
  );
  if (state.searching) {
    go.prepend(spinner());
    go.setAttribute('aria-busy', 'true');
  }
  go.disabled = state.searching || !core.searchable(c.brief);
  go.addEventListener('click', searchRound);
  let hint;
  if (state.searching) {
    hint = `Asking ${state.sources.map((s) => s.label).join(', ')}. This can take half a minute.`;
  } else if (!core.searchable(c.brief)) {
    hint = 'Describe what you are looking for first; the search runs on the brief.';
  } else if (first) {
    hint = 'Runs the brief’s queries against every database.';
  } else if (counts.relevant || counts.excluded) {
    hint =
      `Learns from your ${counts.relevant} relevant and ${counts.excluded} excluded papers: ` +
      'new queries, recommendations and the citation graph around your picks. Nothing already shown comes back.';
  } else {
    hint = 'Screen a few papers first: the next round learns from what you mark.';
  }
  nodes.search.replaceChildren(go, el('p', 'hint', hint));
  // A round asks five databases and can take half a minute; the bar under the
  // button is the page saying it has not stalled.
  if (state.searching) nodes.search.append(el('div', 'lit-bar indeterminate'));
}

async function searchRound() {
  const c = state.current;
  if (state.searching) return;
  state.searching = true;
  renderSearch();
  const res = await api('POST', `/api/literature/${c.id}/rounds`);
  state.searching = false;
  if (state.current !== c) return;
  if (!res.ok) {
    renderSearch();
    nodes.search.append(el('p', 'profile-error', res.message));
    return;
  }
  c.rounds = [...c.rounds, res.data.report];
  state.papers = res.data.papers ?? state.papers;
  state.filter = 'open';
  state.focusId = null;
  renderSearch();
  renderRounds();
  renderPapers();
  loadLists();
  screenLoop();
}

function renderRounds() {
  const rounds = state.current.rounds;
  nodes.rounds.replaceChildren(
    ...rounds
      .slice()
      .reverse()
      .map((report, i) => {
        const { headline, problems } = core.roundSummary(report);
        const box = el('details', 'lit-round');
        box.open = i === 0;
        const head = el('summary', 'lit-round-head', headline);
        if (problems.length) head.append(el('span', 'lit-round-warn', `${problems.length} problem${problems.length === 1 ? '' : 's'}`));
        box.append(head);
        if (report.rationale) box.append(el('p', 'lit-round-why', report.rationale));
        const queries = el('ul', 'lit-round-queries');
        for (const q of report.queries ?? []) queries.append(el('li', '', q));
        box.append(queries);
        const found = el('p', 'hint');
        found.textContent = [
          ...(report.sources ?? []).map((s) => `${s.label} ${s.found}`),
          ...(report.strategies ?? []).map((s) => `${s.label} ${s.found}`),
        ].join(' · ');
        box.append(found);
        const how = core.rankingLabel(report.ranking);
        if (how) box.append(el('p', 'hint', how));
        if (problems.length) {
          const list = el('ul', 'lit-round-problems');
          for (const p of problems) list.append(el('li', '', p));
          box.append(list);
        }
        return box;
      }),
  );
}

// ── screening desk ───────────────────────────────────────────────────

function renderTabs() {
  const counts = core.tally(state.papers);
  const texts = core.fulltextOverview(state.papers, state.files);
  const tabs = [
    ...core.FILTERS.map((filter) => ({ ...filter, count: String(counts[filter.id]) })),
    // Its own view rather than a filter: what it lists is files and what they
    // are missing, not papers to screen.
    { id: 'fulltext', label: 'Full texts', count: `${texts.ready.length}/${texts.relevant}` },
  ];
  nodes.tabs.replaceChildren(
    ...tabs.map((filter) => {
      const tab = el('button', `lit-tab${filter.id === 'fulltext' ? ' lit-tab-texts' : ''}`);
      tab.type = 'button';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(filter.id === state.filter));
      tab.append(el('span', '', filter.label), el('span', 'lit-tab-count', filter.count));
      if (filter.id === 'fulltext' && texts.unassigned.length) {
        tab.append(el('span', 'lit-tab-alert', String(texts.unassigned.length)));
        tab.title = `${texts.unassigned.length} uploaded PDF(s) waiting to be assigned`;
      }
      tab.addEventListener('click', () => {
        if (state.filter !== filter.id) seen.clear();
        state.filter = filter.id;
        state.focusId = null;
        renderPapers();
        screenLoop();
      });
      return tab;
    }),
  );
}

function renderPapers() {
  renderTabs();
  renderProgress();
  if (state.filter === 'fulltext') {
    renderFulltext();
    return;
  }
  const list = core.visible(state.papers, state.filter);
  if (!list.length) {
    const empty = {
      open: state.papers.length ? 'Everything is screened. Search again for more.' : 'No papers yet. Talk through what you need, then search.',
      relevant: 'Nothing marked relevant yet.',
      maybe: 'Nothing marked maybe.',
      excluded: 'Nothing excluded.',
      all: 'No papers yet.',
    }[state.filter];
    nodes.list.replaceChildren(el('p', 'lit-none', empty));
    return;
  }
  if (!list.some((p) => p.id === state.focusId)) state.focusId = null;
  const cards = list.map((paper) => ({ node: card(paper), item: paper }));
  enter(cards, (paper) => `${state.filter}:${paper.id}`);
  nodes.list.replaceChildren(...cards.map(({ node }) => node));
}

function card(paper) {
  const data = paper.data ?? {};
  const node = el('article', 'lit-card');
  node.tabIndex = 0;
  node.dataset.id = String(paper.id);
  if (paper.decision) node.dataset.decision = paper.decision;
  if (paper.id === state.focusId) node.classList.add('focused');
  node.addEventListener('focus', () => setFocus(paper.id, { scroll: false }));

  const title = outbound(data.link || data.url, 'lit-title');
  title.textContent = data.title || 'Untitled';
  const authors = core.authorLine(data.authors);
  const meta = core.metaLine(data);
  node.append(title);
  if (authors) node.append(el('p', 'lit-authors', authors));
  if (meta) node.append(el('p', 'lit-meta', meta));

  const badges = el('div', 'lit-badges');
  const origin = core.originLabel(paper);
  if (origin) badges.append(el('span', 'lit-badge origin', origin));
  const found = core.sourceLine(data.sources);
  if (found) badges.append(el('span', 'lit-badge', found));
  badges.append(el('span', 'lit-badge', `Round ${paper.round}`));
  if (data.pdf) {
    const pdf = outbound(data.pdf, 'lit-badge link');
    pdf.textContent = 'Open-access PDF';
    pdf.title = 'A free copy one of the databases knows of. Download it and add it under Full texts.';
    badges.append(pdf);
  }
  const file = state.files.find((f) => f.paper_id === paper.id);
  if (file) {
    const text = outbound(fileUrl(file), 'lit-badge link fulltext');
    text.textContent = `Full text · ${file.pages} p.`;
    text.title = core.fileLine(file);
    badges.append(text);
  }
  node.append(badges);

  node.append(summaryBlock(paper));

  const abstract = el('details', 'lit-abstract');
  abstract.open = state.openAbstracts.has(paper.id);
  abstract.addEventListener('toggle', () => {
    if (abstract.open) state.openAbstracts.add(paper.id);
    else state.openAbstracts.delete(paper.id);
  });
  abstract.append(el('summary', '', data.abstract ? 'Abstract' : 'No abstract'));
  abstract.append(
    el(
      'p',
      'lit-abstract-text',
      data.abstract || data.tldr || 'None of the databases that found this paper has its abstract.',
    ),
  );
  node.append(abstract);

  const actions = el('div', 'lit-actions');
  for (const d of core.DECISIONS) {
    const b = button(d.label, `lit-decide ${d.id}`, d.icon);
    b.setAttribute('aria-pressed', String(paper.decision === d.id));
    b.title = `${d.label} (${d.key})`;
    b.addEventListener('click', () => decide(paper.id, d.id));
    actions.append(b);
  }
  const note = el('input', 'text-input lit-note');
  note.placeholder = paper.decision === 'excluded' ? 'Why? Steers the next round' : 'Note';
  note.maxLength = 500;
  note.value = paper.note ?? '';
  note.setAttribute('aria-label', 'Note');
  note.addEventListener('change', () => saveNote(paper, note.value));
  actions.append(note);
  if (paper.decision === 'relevant' && !file) {
    const attach = button('Add PDF', 'lit-decide attach', 'upload');
    attach.title = 'Upload this paper’s full text, so the project chat can answer from it';
    attach.addEventListener('click', () => pickFiles({ paperId: paper.id }));
    actions.append(attach);
  }
  node.append(actions);
  return node;
}

function summaryBlock(paper) {
  const box = el('div', 'lit-summary');
  const s = paper.summary;
  if (!s) {
    if (state.loading.has(paper.id)) {
      // Where the summary will stand, at roughly its size, so the card does
      // not jump when it lands.
      const skeleton = el('div', 'lit-skeleton');
      skeleton.setAttribute('aria-label', 'Reading the abstract against your brief');
      skeleton.append(el('span', 'short'), el('span'), el('span', 'mid'));
      box.append(skeleton);
    }
    else if (state.failed.has(paper.id)) box.append(el('p', 'lit-summary-fail', 'No summary: the model did not answer for this paper.'));
    return box;
  }
  if (s.verdict) {
    const label = { relevant: 'Looks relevant', maybe: 'Unclear', excluded: 'Looks out of scope' }[s.verdict];
    box.append(el('span', `lit-verdict ${s.verdict}`, label));
  }
  if (s.summary) box.append(el('p', 'lit-summary-text', s.summary));
  if (s.fit) box.append(el('p', 'lit-fit', s.fit));
  if (s.basis === 'title') box.append(el('p', 'hint', 'No abstract available: judged from the title only.'));
  if (s.basis === 'tldr') box.append(el('p', 'hint', 'No abstract: summarised from the database’s one-line summary.'));
  return box;
}

function cardNode(id) {
  return nodes.list.querySelector(`.lit-card[data-id="${id}"]`);
}

function refreshCard(id) {
  const paper = state.papers.find((p) => p.id === id);
  const node = cardNode(id);
  if (!paper || !node) return;
  const fresh = card(paper);
  node.replaceWith(fresh);
  if (state.focusId === id && document.activeElement === document.body) fresh.focus({ preventScroll: true });
}

function setFocus(id, { scroll = true } = {}) {
  state.focusId = id;
  for (const node of nodes.list.querySelectorAll('.lit-card.focused')) node.classList.remove('focused');
  const node = cardNode(id);
  if (!node) return;
  node.classList.add('focused');
  if (document.activeElement !== node) node.focus({ preventScroll: true });
  if (scroll) node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

async function decide(id, decision) {
  const c = state.current;
  const paper = state.papers.find((p) => p.id === id);
  if (!paper) return;
  const before = paper.decision;
  const next = before === decision ? '' : decision;
  const listBefore = core.visible(state.papers, state.filter);
  paper.decision = next;
  const stays = core.visible(state.papers, state.filter).some((p) => p.id === id);
  state.focusId = stays ? id : core.focusAfter(listBefore, id);
  state.last = { id, before, next, title: paper.data?.title ?? '' };
  const request = api('PATCH', `/api/literature/${c.id}/papers/${id}`, { decision: next });
  // A card that leaves this view folds away instead of vanishing, so the eye
  // sees where it went and that the next one moved up. Short: screening is
  // done at the speed of a key, and the request is already out meanwhile.
  const leaving = !stays && cardNode(id);
  if (leaving && !reducedMotion()) {
    leaving.style.height = `${leaving.offsetHeight}px`;
    leaving.getBoundingClientRect(); // commit the height before it animates away
    leaving.classList.add('lit-leave');
    await wait(180);
    if (state.current !== c) return;
  }
  renderPapers();
  renderUndo();
  if (state.focusId) setFocus(state.focusId);

  const res = await request;
  if (state.current !== c) return;
  if (!res.ok) {
    paper.decision = before;
    state.last = null;
    renderPapers();
    nodes.undo.replaceChildren(el('p', 'profile-error', `Not saved: ${res.message}`));
    return;
  }
  renderSearch();
  loadLists();
}

function renderUndo() {
  const last = state.last;
  if (!last) {
    nodes.undo.replaceChildren();
    return;
  }
  const word = { relevant: 'relevant', maybe: 'maybe', excluded: 'excluded', '': 'not screened' }[last.next];
  const line = el('p', 'lit-undo-line');
  const short = last.title.length > 70 ? `${last.title.slice(0, 70)}…` : last.title;
  line.append(el('span', '', `“${short}” → ${word}. `));
  const undo = el('button', 'lit-link', 'Undo (u)');
  undo.type = 'button';
  undo.addEventListener('click', undoLast);
  line.append(undo);
  nodes.undo.replaceChildren(line);
}

async function undoLast() {
  const last = state.last;
  if (!last) return;
  const paper = state.papers.find((p) => p.id === last.id);
  if (!paper) return;
  state.last = null;
  // Back to exactly what it was, by deciding it again: `decide` toggles, so
  // the previous decision is set directly here.
  const c = state.current;
  paper.decision = last.before;
  state.focusId = last.id;
  if (!core.visible(state.papers, state.filter).some((p) => p.id === last.id)) state.filter = 'all';
  renderPapers();
  renderUndo();
  setFocus(last.id);
  await api('PATCH', `/api/literature/${c.id}/papers/${last.id}`, { decision: last.before });
  renderSearch();
  loadLists();
}

async function saveNote(paper, value) {
  const res = await api('PATCH', `/api/literature/${state.current.id}/papers/${paper.id}`, { note: value });
  if (res.ok) paper.note = value;
}

// ── full texts ───────────────────────────────────────────────────────
//
// The person downloads each PDF with their own access, publishers want their
// own login, and this app holds none, and drops them here. The server reads
// them, matches each to a paper where that is certain, and indexes them; this
// view shows what is ready, what is missing, and what needs a decision.

const fileUrl = (file) => `/api/literature/files/${file.id}/pdf`;

function renderFulltext() {
  const c = state.current;
  const view = core.fulltextOverview(state.papers, state.files);
  const box = el('div', 'lit-texts');

  const head = el('div', 'lit-texts-head');
  head.append(
    el(
      'p',
      'lit-texts-count',
      view.relevant
        ? `${view.ready.length} of ${view.relevant} relevant papers have a full text.`
        : 'Mark papers relevant first: their full texts are what the project chat answers from.',
    ),
  );
  const where = el('p', 'hint');
  if (c.project_id) {
    const project = state.projects.find((p) => p.id === c.project_id);
    where.append(
      `Chats in the project “${project?.name ?? 'this project'}” answer from these pages, ` +
        'cited by page, and from the abstracts of relevant papers without one. ',
    );
    const go = el('button', 'lit-link', 'Open the project chat');
    go.type = 'button';
    go.addEventListener('click', () => {
      // The chat page restores its selected project from here (projects.js).
      try {
        localStorage.setItem('simplixity.project', c.project_id);
      } catch {
        // Private mode: the chat page opens unscoped, one click from the project.
      }
      location.assign('/');
    });
    where.append(go);
  } else {
    where.textContent =
      'File this reading list under a project (top left) to chat with these papers there.';
  }
  head.append(where);
  box.append(head);

  // Drop zone and picker.
  const drop = el('div', 'lit-drop');
  const lead = el('div', 'lit-drop-lead');
  lead.append(iconNode('upload', 18), el('span', 'lit-drop-text', 'Drop PDFs here'));
  const choose = button('Choose files', 'lit-pill', 'file');
  choose.title = 'PDFs of papers in this list: each is matched to its paper.';
  choose.addEventListener('click', () => pickFiles());
  // Papers no round found: the person's own sources. Its own button rather
  // than what every unmatched drop does, since a PDF that merely failed to
  // match its paper would otherwise become a second copy of it.
  const own = button('Add your own papers', 'lit-pill', 'plus');
  own.title = 'PDFs that are not in this list yet. Each becomes a relevant paper, so the project chat answers from it.';
  own.addEventListener('click', () => pickFiles({ own: true }));
  const actions = el('div', 'lit-drop-actions');
  actions.append(choose, own);
  drop.append(
    lead,
    actions,
    el('p', 'hint', 'Matched to their papers by DOI, arXiv id, file name or title; anything uncertain waits below. Papers no search found go in with “Add your own papers”.'),
  );
  box.append(drop);

  if (state.uploading) {
    // Which file, how far through the batch, and a bar that keeps moving
    // while one file is read, looked up and embedded on the server.
    const u = state.uploading;
    const card = el('div', 'lit-upload lit-enter');
    const line = el('div', 'lit-upload-line');
    line.append(spinner(), el('span', 'lit-upload-name', u.name), el('span', 'lit-upload-count', `${u.done + 1} of ${u.total}`));
    const track = el('div', 'lit-bar');
    const fill = el('span', 'lit-bar-fill');
    fill.style.width = `${Math.max(4, (u.done / u.total) * 100)}%`;
    track.append(fill);
    card.append(line, el('p', 'hint', u.own ? 'Reading it, looking it up, indexing its pages' : 'Reading, matching and indexing'), track);
    box.append(card);
  }
  if (state.uploadNotes.length) {
    const notes = el('ul', 'lit-upload-notes');
    for (const note of state.uploadNotes) notes.append(el('li', note.error ? 'error' : '', note.text));
    box.append(notes);
  }
  if (view.unindexed) {
    const line = el(
      'p',
      'lit-progress-line warn',
      `${view.unindexed} passages have no embedding yet and are found by keyword only. `,
    );
    const go = el('button', 'lit-link', state.indexing ? 'Indexing…' : 'Index all');
    go.type = 'button';
    go.disabled = state.indexing;
    if (state.indexing) line.prepend(spinner());
    go.addEventListener('click', indexNow);
    line.append(go);
    box.append(line);
  }

  const rows = [];
  const group = (title, items, build, note = '') => {
    if (!items.length) return;
    box.append(el('h3', 'lit-texts-group', `${title} · ${items.length}`));
    if (note) box.append(el('p', 'hint lit-texts-note', note));
    for (const item of items) {
      const node = build(item);
      rows.push({ node, item });
      box.append(node);
    }
  };
  group('Waiting for you', view.unassigned, unassignedRow);
  group('Missing', view.missing, missingRow);
  group('Ready', view.ready, ({ paper, file }) => readyRow(paper, file));
  group('Not searched', view.idle, ({ paper, file }) => readyRow(paper, file), 'Filed under papers that are not marked relevant, so the chat does not use them.');
  // Keyed by what the row shows, so a file moving from waiting to ready
  // arrives in its new place rather than appearing there unannounced.
  enter(rows, (item) => (item.id && item.name ? `file:${item.id}` : item.paper ? `ready:${item.file.id}:${item.paper.id}` : `missing:${item.id}`));
  nodes.list.replaceChildren(box);
}

function paperTitle(paper) {
  const data = paper.data ?? {};
  const link = outbound(data.link || data.url, 'lit-texts-title');
  link.textContent = data.title || 'Untitled';
  return link;
}

function warnings(file) {
  const list = el('ul', 'lit-texts-warn');
  for (const code of file.warnings ?? []) list.append(el('li', '', core.warningText(code)));
  return list.childElementCount ? list : null;
}

function unassignedRow(file) {
  const row = el('div', 'lit-texts-row waiting');
  const name = outbound(fileUrl(file), 'lit-texts-title');
  name.textContent = file.name;
  row.append(name, el('p', 'hint', core.fileLine(file)));
  const why = warnings(file);
  if (why) row.append(why);

  const choices = core.assignChoices(state.papers, state.candidates[file.id] ?? []);
  const controls = el('div', 'lit-texts-actions');
  const select = el('select', 'lit-texts-select');
  select.setAttribute('aria-label', `Which paper is ${file.name}?`);
  const blank = el('option', '', choices.length ? 'Which paper is this?' : 'No relevant or maybe papers to assign it to');
  blank.value = '';
  select.append(blank);
  for (const paper of choices) {
    const label = `${paper.decision === 'maybe' ? '(maybe) ' : ''}${paper.data?.title ?? 'Untitled'}`;
    const option = el('option', '', label.length > 110 ? `${label.slice(0, 110)}…` : label);
    option.value = String(paper.id);
    select.append(option);
  }
  const pick = el('label', 'lit-pill lit-select-pill lit-texts-pick');
  pick.append(select, iconNode('chevron', 12, 'icon-slot lit-chevron'));
  const assign = button('Assign', 'lit-pill', 'check');
  assign.disabled = true;
  select.addEventListener('change', () => {
    assign.disabled = !select.value;
  });
  assign.addEventListener('click', () => {
    if (select.value) busy(assign, () => assignFile(file, Number(select.value)), { row });
  });
  const adopt = button('New paper', 'lit-pill', 'plus');
  adopt.title = 'None of these: add it to the list as a paper of its own, marked relevant.';
  adopt.addEventListener('click', () => busy(adopt, () => addAsPaper(file), { row }));
  const remove = button('', 'lit-pill lit-icon-pill', 'trash');
  remove.title = 'Delete this PDF';
  remove.setAttribute('aria-label', 'Delete this PDF');
  remove.addEventListener('click', () => deleteFile(file, remove, row));
  controls.append(pick, assign, adopt, remove);
  row.append(controls);
  return row;
}

function missingRow(paper) {
  const row = el('div', 'lit-texts-row missing');
  row.append(paperTitle(paper));
  const meta = [core.authorLine(paper.data?.authors), paper.data?.year].filter(Boolean).join(' · ');
  if (meta) row.append(el('p', 'hint', meta));
  const controls = el('div', 'lit-texts-actions');
  if (paper.data?.pdf) {
    const free = outbound(paper.data.pdf, 'lit-pill');
    free.textContent = 'Open-access copy';
    controls.append(free);
  }
  const attach = button('Add PDF', 'lit-pill', 'upload');
  attach.addEventListener('click', () => pickFiles({ paperId: paper.id }));
  controls.append(attach);
  row.append(controls);
  return row;
}

function readyRow(paper, file) {
  const row = el('div', 'lit-texts-row ready');
  row.append(paperTitle(paper));
  const line = el('p', 'hint');
  const open = outbound(fileUrl(file), 'lit-link');
  open.textContent = file.name;
  line.append(open, document.createTextNode(` · ${core.fileLine(file)}`));
  row.append(line);
  const why = warnings(file);
  if (why) row.append(why);
  const controls = el('div', 'lit-texts-actions');
  const release = button('Wrong file', 'lit-pill', 'undo');
  release.title = 'Take this PDF off the paper. It stays in the list, waiting to be assigned.';
  release.addEventListener('click', () => busy(release, () => assignFile(file, null), { row }));
  const remove = button('', 'lit-pill lit-icon-pill', 'trash');
  remove.title = 'Delete this PDF';
  remove.setAttribute('aria-label', 'Delete this PDF');
  remove.addEventListener('click', () => deleteFile(file, remove, row));
  controls.append(release, remove);
  row.append(controls);
  return row;
}

/** Re-read the list's papers and files after anything changed them. */
async function refreshFiles() {
  const c = state.current;
  const res = await api('GET', `/api/literature/${c.id}`);
  if (!res.ok || state.current !== c) return;
  state.files = res.data.files ?? [];
  state.papers = res.data.papers ?? state.papers;
  renderPapers();
  renderSearch();
  loadLists(); // the sidebar's counts: a new paper, a file filed
}

function pickFiles({ paperId = null, own = false } = {}) {
  const input = el('input');
  input.type = 'file';
  input.accept = 'application/pdf,.pdf';
  input.multiple = paperId == null;
  input.addEventListener('change', () => uploadFiles([...input.files], { paperId, own }));
  input.click();
}

function bindDrop(zone) {
  // The whole screening column takes a drop, not just the box: aiming a
  // dozen files at a small target is the part of this that should not be hard.
  const isFiles = (event) => event.dataTransfer?.types?.includes('Files');
  zone.addEventListener('dragover', (event) => {
    if (!isFiles(event)) return;
    event.preventDefault();
    zone.classList.add('lit-dropping');
  });
  zone.addEventListener('dragleave', (event) => {
    if (!zone.contains(event.relatedTarget)) zone.classList.remove('lit-dropping');
  });
  zone.addEventListener('drop', (event) => {
    if (!isFiles(event)) return;
    event.preventDefault();
    zone.classList.remove('lit-dropping');
    // Dropped on a card: that paper. Anywhere else: matched by the server.
    const cardId = Number(event.target.closest?.('.lit-card')?.dataset.id) || null;
    const files = [...event.dataTransfer.files].filter((f) => /\.pdf$/i.test(f.name) || f.type === 'application/pdf');
    if (!files.length) return;
    uploadFiles(cardId ? files.slice(0, 1) : files, { paperId: cardId });
  });
}

/** One request per file, in order: each is read, matched and embedded on the
 *  server, and a dozen at once would race for the embedding model. */
async function uploadFiles(files, { paperId = null, own = false } = {}) {
  const c = state.current;
  if (!files.length || state.uploading) return;
  state.filter = 'fulltext';
  state.uploadNotes = [];
  // Said once the list is re-read: a paper an upload created is not in
  // `state.papers` until then, and its note would read "nothing matched".
  const landed = [];
  for (let i = 0; i < files.length; i += 1) {
    const file = files[i];
    state.uploading = { done: i, total: files.length, name: file.name, own };
    renderPapers();
    const query = paperId ? `?paper_id=${paperId}` : own ? '?own=true' : '';
    const url = `/api/literature/${c.id}/files${query}`;
    let note;
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/pdf', 'X-Filename': encodeURIComponent(file.name) },
        body: file,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        note = { error: true, text: `${file.name}: ${typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`}` };
      } else {
        landed.push({ name: file.name, data });
        if (data.candidates?.length) state.candidates[data.file.id] = data.candidates;
      }
    } catch {
      note = { error: true, text: `${file.name}: the server is not answering.` };
    }
    if (state.current !== c) return;
    if (note) state.uploadNotes.push(note);
  }
  state.uploading = null;
  await refreshFiles();
  if (state.current !== c) return;
  state.uploadNotes.push(...landed.map(({ name, data }) => uploadNote(name, data)));
  renderPapers();
}

function uploadNote(name, data) {
  const file = data.file ?? {};
  const paper = state.papers.find((p) => p.id === file.paper_id);
  const title = paper?.data?.title ?? '';
  if (!data.new && !data.described) return { text: `${name}: already uploaded${title ? `, filed under “${title}”` : ''}.` };
  const index = data.index?.error ? ` ${data.index.error}` : '';
  if (!paper) {
    const why = data.candidates?.length > 1 ? 'it could be one of several papers' : 'no paper of this list matched it';
    return { error: false, text: `${name}: ${why}. Assign it below.${index}` };
  }
  const described = core.describedLabel(data.described);
  const how = described ? `added as a new paper, ${described}` : core.matchLabel(file.match);
  return { text: `${name} → “${title}” (${how}).${index}` };
}

/** A waiting PDF that is none of the list's papers becomes one of its own. */
async function addAsPaper(file) {
  const c = state.current;
  const res = await api('POST', `/api/literature/${c.id}/files/${file.id}/paper`);
  if (state.current !== c) return;
  delete state.candidates[file.id];
  await refreshFiles();
  if (!res.ok) {
    state.uploadNotes = [{ error: true, text: res.message }];
  } else {
    const paper = state.papers.find((p) => p.id === res.data.file?.paper_id);
    const described = core.describedLabel(res.data.described);
    state.uploadNotes = [
      { text: `${file.name} → “${paper?.data?.title ?? 'new paper'}” (added as a new paper${described ? `, ${described}` : ''}).` },
    ];
  }
  renderPapers();
}

async function assignFile(file, paperId) {
  const c = state.current;
  const res = await api('PATCH', `/api/literature/${c.id}/files/${file.id}`, { paper_id: paperId });
  if (!res.ok) {
    state.uploadNotes = [{ error: true, text: res.message }];
    renderPapers();
    return;
  }
  delete state.candidates[file.id];
  await refreshFiles();
}

async function deleteFile(file, control, row) {
  const ok = await confirmDialog({
    title: `Delete “${file.name}”?`,
    body: 'The PDF and its extracted text are removed. The paper stays in the list.',
    confirm: 'Delete PDF',
  });
  if (!ok) return;
  const c = state.current;
  await busy(control, async () => {
    const res = await api('DELETE', `/api/literature/${c.id}/files/${file.id}`);
    if (!res.ok) return;
    if (!reducedMotion()) {
      row.style.height = `${row.offsetHeight}px`;
      row.getBoundingClientRect();
      row.classList.add('lit-leave');
      await wait(180);
    }
    await refreshFiles();
  }, { row });
}

async function indexNow() {
  const c = state.current;
  state.indexing = true;
  renderPapers();
  // One request embeds a slice (tools/fulltext.py, INDEX_BATCH), so this asks
  // again until nothing waits. Stopping after the first slice would leave a
  // list of four thousand passages needing eight clicks nobody knows to make.
  let res;
  for (;;) {
    res = await api('POST', `/api/literature/${c.id}/index`);
    if (state.current !== c) return;
    if (!res.ok) break;
    state.files = res.data.files ?? state.files;
    const info = res.data.index ?? {};
    if (info.error || !info.waiting || !info.embedded) break;
    renderPapers();
  }
  state.indexing = false;
  if (res.ok) {
    const info = res.data.index ?? {};
    state.uploadNotes = info.error ? [{ error: true, text: info.error }] : [];
  } else {
    state.uploadNotes = [{ error: true, text: res.message }];
  }
  renderPapers();
}

// ── summaries ────────────────────────────────────────────────────────

function renderProgress() {
  const open = state.papers.filter((p) => !p.decision);
  const done = open.filter((p) => p.summary).length;
  nodes.progress.replaceChildren();
  if (!open.length) return;
  if (state.screening) {
    const line = el('p', 'lit-progress-line', `Summarising abstracts: ${done} of ${open.length}`);
    const stop = el('button', 'lit-link', 'Stop');
    stop.type = 'button';
    stop.addEventListener('click', stopScreening);
    line.append(document.createTextNode(' · '), stop);
    nodes.progress.append(line);
  } else if (state.screenError || state.failed.size) {
    const line = el('p', 'lit-progress-line warn', state.screenError || `${state.failed.size} summaries could not be written.`);
    const retry = el('button', 'lit-link', 'Retry');
    retry.type = 'button';
    retry.addEventListener('click', () => {
      state.failed.clear();
      state.screenError = '';
      renderPapers();
      screenLoop();
    });
    line.append(document.createTextNode(' · '), retry);
    nodes.progress.append(line);
  } else if (done < open.length) {
    const line = el('p', 'lit-progress-line', `${open.length - done} without a summary. `);
    const go = el('button', 'lit-link', 'Summarise');
    go.type = 'button';
    go.addEventListener('click', screenLoop);
    line.append(go);
    nodes.progress.append(line);
  }
}

function stopScreening() {
  state.screening?.abort();
  state.screening = null;
  state.loading.clear();
}

async function screenLoop() {
  if (state.screening || !state.current) return;
  const controller = new AbortController();
  state.screening = controller;
  const c = state.current;
  renderProgress();
  try {
    while (!controller.signal.aborted && state.current === c) {
      const ids = core.pendingSummaries(state.papers, state.filter, state.failed).slice(0, BATCH);
      if (!ids.length) break;
      ids.forEach((id) => state.loading.add(id));
      ids.forEach(refreshCard);
      const res = await api('POST', `/api/literature/${c.id}/screen`, { paper_ids: ids }, { signal: controller.signal });
      ids.forEach((id) => state.loading.delete(id));
      if (res.aborted || state.current !== c) break;
      if (!res.ok) {
        ids.forEach((id) => state.failed.add(id));
        state.screenError = `Summaries stopped: ${res.message}`;
        ids.forEach(refreshCard);
        break;
      }
      const summaries = res.data.summaries ?? {};
      for (const id of ids) {
        const paper = state.papers.find((p) => p.id === id);
        if (paper && summaries[String(id)]) paper.summary = summaries[String(id)];
        else state.failed.add(id);
        refreshCard(id);
      }
      renderProgress();
    }
  } finally {
    if (state.screening === controller) state.screening = null;
    if (state.current === c) renderProgress();
  }
}

// ── export ───────────────────────────────────────────────────────────

async function exportBib(withMaybe) {
  const c = state.current;
  const include = withMaybe ? 'relevant,maybe' : 'relevant';
  try {
    const res = await fetch(`/api/literature/${c.id}/export.bib?include=${include}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const name =
      /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') ?? '')?.[1] ?? 'literature.bib';
    const link = el('a');
    link.href = URL.createObjectURL(blob);
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
    const entries = Number(res.headers.get('X-Bib-Entries') ?? 0);
    const registry = Number(res.headers.get('X-Bib-Registry') ?? 0);
    nodes.undo.replaceChildren(
      el(
        'p',
        'lit-undo-line',
        entries
          ? `Exported ${entries} entr${entries === 1 ? 'y' : 'ies'}: ${registry} from the DOI registry, ` +
              `${entries - registry} built from database metadata.`
          : 'Nothing to export yet: mark papers relevant first.',
      ),
    );
  } catch (error) {
    nodes.undo.replaceChildren(el('p', 'profile-error', `Export failed: ${error.message}`));
  }
}

// ── keyboard ─────────────────────────────────────────────────────────

document.addEventListener('keydown', (event) => {
  if (!state.current || isField(event.target) || document.querySelector('dialog[open]')) return;
  const action = core.keyAction(event);
  if (!action) return;
  const list = core.visible(state.papers, state.filter);
  if (!list.length) return;
  const index = list.findIndex((p) => p.id === state.focusId);
  const current = index === -1 ? null : list[index];
  event.preventDefault();
  switch (action) {
    case 'next':
      setFocus(list[Math.min(index + 1, list.length - 1)].id);
      break;
    case 'prev':
      setFocus(list[Math.max(index - 1, 0)].id);
      break;
    case 'relevant':
    case 'maybe':
    case 'excluded':
      if (current) decide(current.id, action);
      else setFocus(list[0].id);
      break;
    case 'abstract': {
      const details = current && cardNode(current.id)?.querySelector('.lit-abstract');
      if (details) details.open = !details.open;
      break;
    }
    case 'open': {
      const link = current && cardNode(current.id)?.querySelector('a.lit-title[href]');
      if (link) window.open(link.href, '_blank', 'noopener,noreferrer');
      break;
    }
    case 'undo':
      undoLast();
      break;
  }
});

// ── boot ─────────────────────────────────────────────────────────────

$('lit-new').addEventListener('click', createList);
window.addEventListener('hashchange', route);

await ensureProfile();
await Promise.all([loadLists(), loadProjects(), loadSettings()]);
route();
