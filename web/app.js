import { subscribe } from './sse.js';
import { BlockView, errorNote } from './blocks.js';
import { hydrate, iconNode } from './icons.js';
import { markDisplayMode, registerServiceWorker } from './pwa.js';
import { Settings } from './settings.js';
import { ChatList } from './chats.js';
import { ProjectList } from './projects.js';
import { DepthChip } from './depth.js';
import { LensChip } from './lenses.js';
import { SourceSwitch, isLibrary, libraryBadge } from './source-switch.js';
import { KEYS, bind, keycap, label, tip } from './shortcuts.js';
import { mountSidebar } from './sidebar.js';
import { ensureProfile, mountProfile } from './profile.js';
import { mountTools } from './tools.js';
import { ProjectBar, ProjectHome } from './project-view.js';
import { Attachments } from './attachments.js';
import { ACCEPT, accepted, fileSummary, filesBadge } from './attachments-core.js';
import { filesByMessage, modelLabel, stamp } from './turns-core.js';

hydrate();
markDisplayMode();
registerServiceWorker();

const $ = (id) => document.getElementById(id);

const app = $('app');
const form = $('composer');
const input = $('query');
const output = $('output');
const welcome = $('welcome');
const scroll = $('scroll');
const sendBtn = $('send');

// One place for "which chat am I in and what is streaming", so nothing has to
// guess by reading the DOM.
// `nav` counts navigations: anything that awaits checks it afterwards, so a
// chat opened, or a question sent, while the page was busy with another one
// cannot land in the chat now on screen.
const state = { chatId: null, chatProject: null, runId: null, subscription: null, nav: 0 };

const settings = new Settings();
const depthChip = new DepthChip($('depth-chip'), (depth) => settings.patch({ depth }));
const lensChip = new LensChip($('lens-chip'), (lenses) => settings.patch({ lenses }));
// Per project and per page load only, see source-switch.js for why.
const webByProject = new Map();
// Whether the project's earlier chats are searched too. Off on entering a
// project, like the web switch, and for the same reason: an earlier answer is
// the reader's own conclusion, and building on it is a choice made per
// question, not a setting that lingers. The server holds the same line.
const chatsByProject = new Map();
const chatsToggle = $('chats-toggle');
chatsToggle.addEventListener('click', () => {
  const project = activeProject();
  if (!project) return;
  chatsByProject.set(project.id, !chatsByProject.get(project.id));
  syncComposer();
});
const sourceSwitch = new SourceSwitch($('source-switch'), (web) => {
  webByProject.set(webKey(), web);
  syncComposer();
});

// Files attached to the open chat. With any, the chat answers from them, so
// the composer shows the Local/Web switch in place of depth and sources, as
// in a literature project, and for the same reasons (attachments.js).
const attachments = new Attachments($('attachments'), { onChange: () => syncComposer() });
const attachInput = $('attach-input');
attachInput.accept = ACCEPT;
$('attach').title = 'Attach PDF, Word, Markdown or text files: the answer is written from them';
$('attach').addEventListener('click', () => attachInput.click());
attachInput.addEventListener('change', () => {
  attachFiles([...attachInput.files]);
  attachInput.value = '';
});

function attachFiles(files) {
  const usable = files.filter((file) => accepted(file.name));
  const refused = files.filter((file) => !accepted(file.name)).map((file) => file.name);
  if (refused.length) attachments.refuse(refused, 'Only PDF, Word (.docx), Markdown and text files can be attached.');
  if (usable.length) attachments.add(usable, state.chatId);
  input.focus();
}

// Dropped on the composer, or pasted into it, a file is attached the same way.
form.addEventListener('dragover', (event) => {
  if (!event.dataTransfer?.types?.includes('Files')) return;
  event.preventDefault();
  form.classList.add('dropping');
});
form.addEventListener('dragleave', (event) => {
  if (!form.contains(event.relatedTarget)) form.classList.remove('dropping');
});
form.addEventListener('drop', (event) => {
  form.classList.remove('dropping');
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  attachFiles([...event.dataTransfer.files]);
});
input.addEventListener('paste', (event) => {
  const files = [...(event.clipboardData?.files ?? [])];
  if (!files.length) return;
  event.preventDefault();
  attachFiles(files);
});
const chatList = new ChatList($('chat-list'), {
  onOpen: openChat,
  onDelete: (id) => id === state.chatId && startNewChat(),
});

// Selecting a project does two things at once, and they are the same thing: it
// filters the list below, and it scopes the next question: a new chat is filed
// there, and the answers already written there are searched with the web.
// And it opens the project's page (project-view.js), so the bar above never
// names one project while the sidebar has selected another.
const projectList = new ProjectList($('project-list'), {
  onSelect: (projectId) => {
    chatList.setProject(projectId, projectList.projects);
    startNewChat();
  },
  // A chat dragged onto a project: the counts and the filtered list below are
  // both stale now, and the move has already been written.
  onDrop: (chatId, projectId) => {
    if (chatId === state.chatId) state.chatProject = projectId;
    return projectList.load().then(() => {
      chatList.setProject(projectList.selectedId, projectList.projects);
      syncComposer();
    });
  },
});
// A chat moved into or out of a literature project changes what it answers
// from, so the composer has to follow the open chat's new home.
chatList.onMove = async (chatId, projectId) => {
  if (chatId === state.chatId && projectId !== undefined) state.chatProject = projectId;
  await projectList.load();
  syncComposer();
};

const projectBar = new ProjectBar($('project-bar'), {
  settings,
  // The name leads to the project's page, the cross leaves the project. The
  // open chat may belong to a project the sidebar has not selected (it was
  // opened from search), so the page is reached by selecting it.
  onHome: () => {
    const project = activeProject();
    if (!project) return;
    if (projectList.selectedId === project.id) startNewChat();
    else projectList.select(project.id);
  },
  onLeave: () => projectList.select(null),
});

const projectHome = new ProjectHome($('project-home'), {
  onOpenChat: (chatId) => openChat(chatId),
  onRename: async (project) => {
    await projectList.rename(project);
    syncComposer();
    showLanding();
  },
  onLeave: () => projectList.select(null),
  onAsk: () => input.focus(),
});

/** What the page shows when no chat is open: the project's page inside a
 *  project, the welcome everywhere else. */
function showLanding() {
  const project = activeProject();
  if (project) {
    welcome.hidden = true;
    projectHome.show(project);
  } else {
    projectHome.hide();
    welcome.hidden = false;
  }
}

/** The project the next question belongs to: the open chat's, or for a new
 *  chat the one selected in the sidebar. */
function activeProject() {
  const id = state.chatId ? state.chatProject : projectList.selectedId;
  return projectList.projects.find((p) => p.id === id) ?? null;
}

/** Says which project the next question lands in, above the composer, and
 *  shows the controls that project takes. A literature project has one: the
 *  Local/Web switch, in place of depth and sources (source-switch.js). */
function updateProjectBadge() {
  syncComposer();
}

/** Where the Web switch's position is remembered: per literature project, or
 *  per chat for one that is about its attached files. */
function webKey() {
  const project = activeProject();
  return isLibrary(project) ? project.id : `chat:${state.chatId ?? attachments.chatId ?? 'new'}`;
}

function syncComposer() {
  const badge = $('project-badge');
  const project = activeProject();
  const library = isLibrary(project);
  const files = attachments.count;
  const bound = library || files > 0;
  const web = bound && Boolean(webByProject.get(webKey()));
  $('depth-chip').hidden = bound;
  $('lens-chip').hidden = bound;
  $('source-switch').hidden = !bound;
  sourceSwitch.set(web);
  if (library) sourceSwitch.setTip(libraryBadge(project, web));
  else sourceSwitch.setTip(filesBadge(files, web), 'the attached files');
  badge.hidden = true;
  const chats = Boolean(project && chatsByProject.get(project.id));
  chatsToggle.hidden = !project;
  chatsToggle.setAttribute('aria-pressed', String(chats));
  chatsToggle.title = chats
    ? 'Searching this project’s earlier chats as well; they are cited as “This project”. Click to leave them out.'
    : 'Click to search this project’s earlier chats as well, for continuity with what you already worked out.';
  projectBar.set(project);
  form.dataset.library = String(bound);
  input.placeholder = library
    ? web
      ? 'Ask your papers and the web…'
      : 'Ask your papers…'
    : files
      ? web
        ? 'Ask your files and the web…'
        : 'Ask your files…'
      : 'Ask anything…';
}

// ── chrome ───────────────────────────────────────────────────────────

// One button for both jobs: it submits while idle and cancels while a run is
// in flight. Switching `type` keeps the two paths out of each other's way,
// as a submit button the form's own handler fires, as a plain button only the
// cancel listener below does.
function setBusy(busy) {
  sendBtn.type = busy ? 'button' : 'submit';
  sendBtn.dataset.state = busy ? 'stop' : 'send';
  sendBtn.setAttribute('aria-label', busy ? 'Stop' : 'Send');
  sendBtn.title = busy ? 'Stop' : `Send · ${label('enter')}`;
  // Return only ever sends. While a run is up the button is the only way to
  // stop it, so the keycap beside it would be a lie.
  sendHint.hidden = busy;
  sendBtn.disabled = false;
}

// Sits in the composer bar rather than on the button: .send clips its own
// children to a 32px circle to keep the sheen inside it.
const sendHint = keycap('enter');
sendBtn.before(sendHint);

// Follow the stream only while the user is already at the bottom; the moment
// they scroll up to read something, stop yanking the viewport around.
let pinned = true;
scroll.addEventListener('scroll', () => {
  pinned = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
});
const stickToBottom = () => {
  if (pinned) scroll.scrollTop = scroll.scrollHeight;
};

// ── turns ────────────────────────────────────────────────────────────

/** A small grey line of time (and model, where one is given). Each part is
 *  left out when unknown, and the line is hidden while it has none. */
function metaLine(className) {
  const line = document.createElement('div');
  line.className = `msg-meta ${className}`;
  const time = document.createElement('time');
  const model = document.createElement('span');
  model.className = 'msg-model';
  line.append(time, model);
  const set = ({ at, by } = {}) => {
    if (at !== undefined) {
      const { text, title } = stamp(at);
      time.textContent = text;
      time.title = title;
      if (text) time.dateTime = new Date(at).toISOString();
    }
    if (by !== undefined) {
      model.textContent = modelLabel(by);
      model.title = by ?? '';
    }
    line.hidden = !time.textContent && !model.textContent;
  };
  set({});
  return { line, set };
}

/** The files a question went out with, inside its bubble, each opening in the
 *  reader. Shown there rather than left in the composer, where they looked as
 *  if they had never been sent. */
// Files named on a question before the rest fold into "+N Paper": one row,
// however many were sent. Eight chips stacked four rows high above a one-line
// question buried the question.
const SHOWN_FILES = 2;

function fileList(files) {
  const list = document.createElement('ul');
  list.className = 'msg-files';
  const folded = files.length > SHOWN_FILES;
  const shown = folded ? files.slice(0, SHOWN_FILES) : files;
  list.append(...shown.map(fileItem));
  if (folded) {
    list.classList.add('folded');
    const rest = files.slice(SHOWN_FILES);
    const item = document.createElement('li');
    item.className = 'msg-files-more';
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'msg-file';
    more.textContent = `+${rest.length} Paper`;
    more.title = rest.map((file) => file.name).join('\n');
    more.addEventListener('click', () => {
      list.classList.remove('folded');
      item.replaceWith(...rest.map(fileItem));
    });
    item.append(more);
    list.append(item);
  }
  return list;
}

function fileItem(file) {
  const item = document.createElement('li');
  const link = document.createElement('a');
  link.className = 'msg-file';
  link.href = `/library/${encodeURIComponent(file.id)}/1`;
  link.target = '_blank';
  link.rel = 'noopener';
  link.title = `${file.name}\n${fileSummary(file)}`;
  const name = document.createElement('span');
  name.textContent = file.name;
  link.append(iconNode('file', 14), name);
  item.append(link);
  return item;
}

/**
 * @param {string} query
 * @param {{ files?: object[], sentAt?: string|Date, model?: string, answeredAt?: string|Date }} meta
 */
function newTurn(query, { files = [], sentAt, model, answeredAt } = {}) {
  welcome.hidden = true;
  projectHome.hide();
  const turn = document.createElement('article');
  turn.className = 'turn';
  if (query) {
    const ask = document.createElement('div');
    ask.className = 'user-turn';
    const bubble = document.createElement('div');
    bubble.className = 'user-msg';
    if (files.length) bubble.append(fileList(files));
    const text = document.createElement('div');
    text.className = 'user-text';
    text.textContent = query;
    bubble.append(text);
    const sent = metaLine('user-meta');
    sent.set({ at: sentAt });
    ask.append(bubble, sent.line);
    turn.append(ask);
  }
  const blocks = document.createElement('div');
  blocks.className = 'turn-blocks';
  turn.append(blocks);
  output.append(turn);
  const view = new BlockView(blocks);
  // When the answer was written and by which model: the last icon of the
  // turn's tray, with the full line as its tooltip. Notes about a failed
  // or stopped run go above the tray (`view.note`), where they are read.
  const meta = { at: answeredAt, by: model };
  const answered = (patch) => {
    Object.assign(meta, patch);
    const time = stamp(meta.at);
    const by = modelLabel(meta.by);
    const text = [time.text, by].filter(Boolean).join(' · ');
    const title = [time.title, meta.by].filter(Boolean).join(' · ');
    view.setMeta({ text, title });
  };
  answered({});
  return { turn, view, answered, note: (node) => view.note(node) };
}

function detach() {
  state.subscription?.close();
  state.subscription = null;
  state.runId = null;
  setBusy(false);
}

function attach(runId, view, note, answered) {
  state.runId = runId;
  setBusy(true);
  pinned = true;

  state.subscription = subscribe(runId, {
    onChange: (blocks) => {
      view.render(blocks);
      stickToBottom();
    },
    onPhase: (phase) => view.setPhase(phase),
    onDone: ({ status, message }) => {
      setBusy(false);
      state.runId = null;
      view.settle();
      answered({ at: new Date() });
      if (status === 'error') note(errorNote(message ?? 'The run failed.'));
      if (status === 'cancelled') note(errorNote(message ?? 'Stopped.', { level: 'stopped' }));
      // The title is written after the run completes, so the sidebar is only
      // right once it has.
      chatList.load();
      stickToBottom();
    },
  });
}

// ── sending ──────────────────────────────────────────────────────────

// Set while a question waits for its files to finish uploading, so a second
// Return in that gap does not send it twice.
let waitingForFiles = false;

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = input.value.trim();
  if (!query || state.runId || waitingForFiles) return;
  const nav = state.nav;
  if (attachments.uploading) {
    // Asked while a file is still being read: the answer waits for it rather
    // than being written without it.
    waitingForFiles = true;
    sendBtn.disabled = true;
    await attachments.settled();
    sendBtn.disabled = false;
    waitingForFiles = false;
    // Left for another chat while the files were read: the question stays in
    // the composer rather than being sent into whatever is open now.
    if (nav !== state.nav) {
      return;
    }
  }

  input.value = '';
  autoGrow();
  // What this question carries, taken now: a file attached while the request
  // is on its way waits for the next one.
  const files = attachments.draft;
  const { view, note, answered } = newTurn(query, { files, sentAt: new Date() });
  stickToBottom();

  let response;
  try {
    response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // Model and depth come from the saved settings unless this chat is
      // already open, so the picker stays the single source of truth.
      body: JSON.stringify({
        query,
        // A new chat with files attached already has an id: the one they
        // were uploaded under.
        chat_id: state.chatId ?? attachments.idForQuestion(),
        depth: settings.values?.depth,
        lenses: settings.values?.lenses,
        // Read only in a literature project, where it is the whole choice;
        // the server ignores depth and lenses there.
        web: sourceSwitch.web,
        // The project's earlier chats, only when the Chats switch is on.
        chats: chatsToggle.getAttribute('aria-pressed') === 'true',
        // Only for a chat that does not exist yet; the server ignores it for
        // one that does, so a follow-up never moves the chat it belongs to.
        project_id: state.chatId ? null : projectList.selectedId,
      }),
    });
  } catch {
    note(errorNote('Could not reach the server.'));
    return;
  }

  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    note(errorNote(detail?.detail ?? `Could not start the run (HTTP ${response.status}).`));
    return;
  }

  const { run_id, chat_id, model } = await response.json();
  if (nav !== state.nav) {
    // Another chat was opened while this one was starting. The run goes on
    // and is there to rejoin; this page is showing something else now.
    chatList.load();
    return;
  }
  attachments.markSent(chat_id, files);
  answered({ by: model });
  if (chat_id !== state.chatId) {
    state.chatProject = projectList.selectedId;
    state.chatId = chat_id;
    location.hash = `chat=${chat_id}`;
    chatList.load().then(() => chatList.setActive(chat_id));
  }
  attach(run_id, view, note, answered);
});

sendBtn.addEventListener('click', async () => {
  if (!state.runId) return; // idle: type=submit, the form's submit handler sends
  sendBtn.disabled = true;
  await fetch(`/api/runs/${state.runId}/cancel`, { method: 'POST' }).catch(() => {});
});

// ── composer ─────────────────────────────────────────────────────────

function autoGrow() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
}
input.addEventListener('input', autoGrow);

input.addEventListener('keydown', (event) => {
  // Enter sends, Shift+Enter breaks the line: the convention people expect.
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

for (const chip of document.querySelectorAll('.suggestion')) {
  chip.addEventListener('click', () => {
    input.value = chip.dataset.query ?? chip.textContent.trim();
    autoGrow();
    input.focus();
  });
}

// ── chats ────────────────────────────────────────────────────────────

const runExists = (runId) =>
  fetch(`/api/runs/${runId}`)
    .then((res) => res.ok)
    .catch(() => false);

async function openChat(chatId) {
  if (chatId === state.chatId && output.childElementCount) return;
  detach();
  const nav = ++state.nav;

  // Missing and unreachable are different answers: only a chat the server
  // says does not exist gives way to a new one. A failed request used to do
  // the same, and a server mid-restart looked like a deleted chat.
  let chat = null;
  let failure = null;
  try {
    const res = await fetch(`/api/chats/${chatId}`);
    if (res.ok) chat = await res.json();
    else if (res.status !== 404) failure = `Could not open this chat (HTTP ${res.status}).`;
  } catch {
    failure = 'Could not reach the server to open this chat.';
  }
  if (nav !== state.nav) return;

  if (!chat) {
    startNewChat();
    if (failure) output.append(errorNote(failure));
    return;
  }

  state.chatId = chatId;
  state.chatProject = chat.project_id ?? null;
  const { perMessage, draft } = filesByMessage(chat.messages, chat.files ?? []);
  attachments.set(chatId, chat.files ?? [], draft);
  syncComposer();
  location.hash = `chat=${chatId}`;
  chatList.setActive(chatId);
  output.replaceChildren();
  welcome.hidden = true;
  projectHome.hide();

  for (const [index, message] of chat.messages.entries()) {
    const { view, note, answered } = newTurn(message.query, {
      files: perMessage[index],
      sentAt: message.created_at,
      model: message.model,
      answeredAt: message.finished_at,
    });
    view.render(message.blocks ?? []);
    view.settle();
    // A run still in flight (a reload during deep research lands here) is
    // rejoined rather than shown as finished. Its event stream expires after a
    // day though, so rejoining is only offered while the run still exists;
    // subscribing to a vanished run would retry forever behind a spinner.
    const live = message.status === 'running' && message.run_id && (await runExists(message.run_id));
    // Another chat was opened while this one was being rebuilt.
    if (nav !== state.nav) return;
    if (live) {
      attach(message.run_id, view, note, answered);
    } else if (message.status === 'running' || message.status === 'error') {
      // The transcript now carries the reason as a notice block (see
      // runs._with_reason). The generic sentence is the fallback for the runs
      // that never got to write one (a hard kill, or a transcript stored
      // before the reason was persisted at all.
      const explained = (message.blocks ?? []).some(
        (block) => block.type === 'notice' && block.data?.level === 'error',
      );
      if (!explained) note(errorNote('This run did not finish.'));
    }
  }

  pinned = true;
  stickToBottom();
  input.focus();
}

function startNewChat() {
  detach();
  state.nav += 1;
  state.chatId = null;
  state.chatProject = null;
  attachments.reset();
  syncComposer();
  history.replaceState(null, '', location.pathname);
  output.replaceChildren();
  showLanding();
  chatList.setActive(null);
  input.focus();
}

$('new-chat').addEventListener('click', startNewChat);
chatList.bindSearch($('chat-search'));

// ── shell ────────────────────────────────────────────────────────────

const toggleSidebar = mountSidebar(app);
$('toggle-sidebar').addEventListener('click', toggleSidebar);
$('scrim').addEventListener('click', toggleSidebar);

const overlay = $('settings-overlay');
const openSettings = () => {
  overlay.hidden = false;
  $('close-settings').focus();
};
const closeSettings = () => (overlay.hidden = true);
$('open-settings').addEventListener('click', openSettings);
$('close-settings').addEventListener('click', closeSettings);
overlay.addEventListener('click', (event) => {
  if (event.target === overlay) closeSettings();
});

// ── keyboard ─────────────────────────────────────────────────────────
//
// Every action in the chrome has a key, and every key is printed next to the
// thing it does: beside the label where there is one, in the tooltip where the
// control is an icon. Both come from the same table in shortcuts.js, and the
// stylesheet keeps the printing off touch screens, where it would be a claim
// about a keyboard that is not there.

bind({
  [KEYS.sidebar]: toggleSidebar,
  [KEYS.search]: () => $('chat-search').focus(),
  [KEYS.newChat]: startNewChat,
  [KEYS.newProject]: () => projectList.create(),
  [KEYS.settings]: () => (overlay.hidden ? openSettings() : closeSettings()),
});

// Escape stays out of that table: it means "back out of the nearest thing",
// and which thing that is depends on what is open. A modal <dialog> handles
// its own, so by the time this runs the settings overlay is the only claimant
// left, unless the search field has focus, which is nearer.
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  const search = $('chat-search');
  if (document.activeElement === search) {
    if (search.value) {
      search.value = '';
      search.dispatchEvent(new Event('input'));
    } else {
      input.focus();
    }
    return;
  }
  if (!overlay.hidden) closeSettings();
});

// A keycap wherever there is room for one, a tooltip where there is not. New
// chat is the action people reach for most, so its key is printed in the topbar
// rather than left to be discovered by hovering a 32-pixel square.
tip($('toggle-sidebar'), KEYS.sidebar);
tip($('new-chat'), KEYS.newChat);
tip($('close-settings'), 'escape');
$('new-chat').after(keycap(KEYS.newChat));
$('chat-search').closest('.sidebar-search').append(keycap(KEYS.search));
$('open-settings').append(keycap(KEYS.settings));

// ── boot ─────────────────────────────────────────────────────────────

setBusy(false);

// Before anything personal loads: until a profile is chosen the server answers
// every one of those requests with a 401, and the picker is what fixes that.
mountProfile($('switch-profile'), $('profile-name'), await ensureProfile());

// Keep the composer chip and the modal's depth select showing the same thing.
settings.onChange = (values) => {
  depthChip.set(values.depth);
  lensChip.set(values.lenses);
  projectBar.sync();
};
await settings.init();
// After settings: the redact tool remembers its local model there.
mountTools($('open-tools'), { settings });
await depthChip.init(settings.values.depth);
await lensChip.init(settings.values.lenses);
// Projects first: the chat list is filtered by the remembered selection, and
// loading it before that is known would show everything and then flicker.
await projectList.load();
chatList.projectId = projectList.selectedId;
chatList.projects = projectList.projects;
updateProjectBadge();
await chatList.load();

const requested = new URLSearchParams(location.hash.slice(1)).get('chat');
if (requested) await openChat(requested);
else {
  showLanding();
  input.focus();
}
