// The sidebar's project list, and the menu that files a chat into one.
//
// A project is a folder for chats *and* the corpus made out of them: selecting
// one filters the list below and scopes the next question to it, so the answers
// already written there are searched alongside the web (`app/search/project.py`).
// Both halves are the same selection, which is why one small module owns it.

import { iconNode } from './icons.js';
import { confirmDialog, promptDialog } from './dialog.js';
import { KEYS, keycap, label } from './shortcuts.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};


/** Both, because a Mac keyboard has no Del: the key under the finger reaching
 *  to delete a row reports itself as Backspace there and as Delete on a PC. */
export const DELETE_KEYS = new Set(['Delete', 'Backspace']);

/** Selection survives a reload: a project is a place you work in for days, and
 *  landing back in "everything" every morning is how it stops being one. */
const SELECTED_KEY = 'simplixity.project';

/** The drag payload is a chat id under a private type, so a project row can tell
 *  a chat from a link or a file *during* the drag: `getData` is blocked until
 *  the drop, but `types` is readable throughout, which is what decides whether
 *  the row lights up as a target. */
export const CHAT_MIME = 'application/x-simplixity-chat';

/** Files a chat into a project, or out of every project with null. The one
 *  writer for both the drag-and-drop and the keyboard path below. */
export async function moveChat(chatId, projectId) {
  try {
    const res = await fetch(`/api/chats/${chatId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId ?? '' }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

const remember = (id) => {
  try {
    if (id) localStorage.setItem(SELECTED_KEY, id);
    else localStorage.removeItem(SELECTED_KEY);
  } catch {
    // Private mode. The selection still holds for this session.
  }
};

const recall = () => {
  try {
    return localStorage.getItem(SELECTED_KEY);
  } catch {
    return null;
  }
};

export class ProjectList {
  constructor(root, { onSelect, onDrop } = {}) {
    this.root = root;
    this.onSelect = onSelect;
    this.onDrop = onDrop;
    this.projects = [];
    this.selectedId = recall();
  }

  get selected() {
    return this.projects.find((p) => p.id === this.selectedId) ?? null;
  }

  async load() {
    try {
      const res = await fetch('/api/projects');
      if (!res.ok) return;
      this.projects = (await res.json()).projects ?? [];
    } catch {
      return; // offline or mid-redeploy; the sidebar is not worth an error
    }
    // A project deleted in another tab must not leave the list filtered by an
    // id nothing matches. That reads as "no chats" and looks like data loss.
    if (this.selectedId && !this.projects.some((p) => p.id === this.selectedId)) {
      this.select(null);
      return;
    }
    this.render();
  }

  select(id) {
    this.selectedId = id ?? null;
    remember(this.selectedId);
    this.render();
    this.onSelect?.(this.selectedId);
  }

  async create() {
    const name = await promptDialog({
      title: 'New project',
      body: 'A folder for chats, and a corpus: with Chats switched on in the composer, what you have already answered here is searched alongside the web.',
      label: 'Project name',
      placeholder: 'Thesis, house move, EU AI Act…',
      confirm: 'Create project',
    });
    if (!name) return;
    const res = await fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!res.ok) return;
    const project = await res.json();
    await this.load();
    this.select(project.id);
  }

  async rename(project) {
    const name = await promptDialog({
      title: 'Rename project',
      label: 'Project name',
      value: project.name,
      confirm: 'Rename',
    });
    if (!name || name === project.name) return;
    await fetch(`/api/projects/${project.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    await this.load();
  }

  async remove(project) {
    // Said in full, because the answer is not the obvious one: the chats stay.
    const ok = await confirmDialog({
      title: `Delete the project “${project.name}”?`,
      body: 'Its chats are kept, unfiled.',
      confirm: 'Delete project',
    });
    if (!ok) return;
    const res = await fetch(`/api/projects/${project.id}`, { method: 'DELETE' });
    if (!res.ok) return;
    if (this.selectedId === project.id) this.selectedId = null;
    remember(this.selectedId);
    await this.load();
    this.onSelect?.(this.selectedId);
  }

  render() {
    const nodes = [];

    const head = el('div', 'projects-head');
    head.append(el('span', 'projects-label', 'Projects'));
    const add = el('button', 'icon-btn small');
    add.type = 'button';
    add.title = `New project · ${label(KEYS.newProject)}`;
    add.setAttribute('aria-label', 'New project');
    add.append(iconNode('plus', 14));
    add.addEventListener('click', () => this.create());
    // The keycap goes in the row itself, beside the button: this section is
    // where a first project gets made, and a shortcut nobody can see until
    // they hover the right 24 pixels may as well not exist.
    head.append(keycap(KEYS.newProject), add);
    nodes.push(head);

    // No "All chats" row: everything is the resting state, not a place to go.
    // Clicking the selected project steps back out of it, which is the same
    // gesture as leaving a folder and needs no row of its own.
    for (const project of this.projects) {
      nodes.push(this.#item(project));
    }
    this.root.replaceChildren(...nodes);
  }

  #item(project) {
    const selected = project.id === this.selectedId;
    const row = el('div', `project-item${selected ? ' active' : ''}`);

    const open = el('button', 'project-open');
    open.type = 'button';
    open.title = selected ? `Leave ${project.name}` : project.name;
    open.append(
      iconNode('folder', 14),
      el('span', 'project-name', project.name),
      el('span', 'project-count', String(project.chats ?? 0)),
    );
    open.addEventListener('click', () => this.select(selected ? null : project.id));

    // The row's two other actions, from the row itself. Both live behind a
    // hover button otherwise, which a keyboard can reach by tabbing but only
    // after passing through every row above it; F2 and Delete are what a file
    // list has meant since long before this one.
    open.addEventListener('keydown', (event) => {
      if (event.key === 'F2') {
        event.preventDefault();
        this.rename(project);
      } else if (DELETE_KEYS.has(event.key)) {
        event.preventDefault();
        this.remove(project);
      }
    });

    // Drop a chat here to file it. `dragleave` also fires when the pointer
    // crosses onto a child of the row, so the highlight is only dropped when
    // the pointer has genuinely left it.
    row.addEventListener('dragover', (event) => {
      if (!event.dataTransfer?.types.includes(CHAT_MIME)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      row.classList.add('drop-target');
    });
    row.addEventListener('dragleave', (event) => {
      if (!row.contains(event.relatedTarget)) row.classList.remove('drop-target');
    });
    row.addEventListener('drop', async (event) => {
      if (!event.dataTransfer?.types.includes(CHAT_MIME)) return;
      event.preventDefault();
      row.classList.remove('drop-target');
      const chatId = event.dataTransfer.getData(CHAT_MIME);
      if (!chatId || !(await moveChat(chatId, project.id))) return;
      this.onDrop?.(chatId, project.id);
    });

    const menu = el('button', 'project-menu');
    menu.type = 'button';
    menu.title = `Rename (${label('f2')}) · Shift-click to delete (${label('delete')})`;
    menu.setAttribute('aria-label', `Rename or delete ${project.name}`);
    menu.append(iconNode('settings', 13));
    menu.addEventListener('click', (event) => {
      event.stopPropagation();
      // Two actions is not a menu. Shift-click deletes, click renames, and the
      // title says so. A popover here would be more code than the feature.
      if (event.shiftKey) this.remove(project);
      else this.rename(project);
    });
    // Keyboard activation reports the modifiers in the click it synthesises,
    // but only in some browsers, so the shift half is read from the key event
    // instead and the click it would have produced is cancelled.
    menu.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      if (event.shiftKey) this.remove(project);
      else this.rename(project);
    });

    row.append(open, menu);
    return row;
  }
}

/** The "file this chat under…" control on a chat row.
 *
 *  Dragging the chat onto a project is the gesture people reach for first; this
 *  is the other half of it: the one that works from the keyboard, on a touch
 *  screen, and for taking a chat *out* of a project, which has no folder to
 *  drag onto. A native <select>, because the list is short, and a custom
 *  popover would be the third dropdown implementation in this codebase. */
export function projectPicker(chat, projects, onChange) {
  const wrap = el('label', 'chat-project');
  wrap.title = 'Move to a project';
  wrap.append(iconNode('folder-in', 14));

  const select = el('select', 'chat-project-select');
  select.setAttribute('aria-label', `Move ${chat.title} to a project`);
  const none = el('option', null, 'No project');
  none.value = '';
  select.append(none);
  for (const project of projects) {
    const option = el('option', null, project.name);
    option.value = project.id;
    if (project.id === chat.project_id) option.selected = true;
    select.append(option);
  }
  select.addEventListener('click', (event) => event.stopPropagation());
  select.addEventListener('change', async (event) => {
    event.stopPropagation();
    if (await moveChat(chat.id, select.value || null)) onChange?.(select.value || null);
  });

  wrap.append(select);
  return wrap;
}
