// The sidebar chat list.
//
// Search is server-side (it looks inside message text, not just titles), so the
// input is debounced rather than filtering a local array.

import { icon } from '../shared/icons.js';
import { confirmDialog } from '../shared/dialog.js';
import { CHAT_MIME, DELETE_KEYS, projectPicker } from '../projects/projects.js';
import { label } from '../shared/shortcuts.js';

const DEBOUNCE_MS = 250;

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

const startOfDay = (date) =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();

/** Buckets by recency, because a flat list of forty titles is unreadable.
 *
 *  By calendar day, not by elapsed hours. Counting hours is why everything sat
 *  under "Today": a chat from yesterday evening is fourteen hours old at
 *  breakfast, and "Yesterday" only ever meant "between 24 and 48 hours ago",
 *  which is a window that has nothing to do with what yesterday means. Local
 *  midnight is the boundary the reader has in mind, so it is the one used,
 *  the timestamps are UTC and `Date` converts them on the way in. */
export function bucket(updatedAt) {
  const then = new Date(updatedAt);
  if (Number.isNaN(then.getTime())) return 'Earlier';
  const days = Math.round((startOfDay(new Date()) - startOfDay(then)) / 86400000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return 'Previous 7 days';
  if (days < 30) return 'Previous 30 days';
  return 'Earlier';
}

export class ChatList {
  constructor(root, { onOpen, onDelete } = {}) {
    this.root = root;
    this.onOpen = onOpen;
    this.onDelete = onDelete;
    this.activeId = null;
    this.search = '';
    this.timer = null;
    this.chats = [];
    // Which project the list is showing, and the projects a chat can be moved
    // into. Both are owned by `ProjectList`; this only renders them.
    this.projectId = null;
    this.projects = [];
  }

  /** Filter the list to one project, or to everything with null. */
  setProject(projectId, projects = this.projects) {
    this.projectId = projectId ?? null;
    this.projects = projects;
    return this.load();
  }

  bindSearch(input) {
    input.addEventListener('input', () => {
      this.search = input.value.trim();
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.load(), DEBOUNCE_MS);
    });
  }

  async load() {
    const params = new URLSearchParams();
    if (this.search) params.set('search', this.search);
    if (this.projectId) params.set('project_id', this.projectId);
    // Only the latest request may render. Typing "kafka" fires a search per
    // pause, and the answer for "ka" arriving after the one for "kafka" put
    // the wrong list on screen under the right query.
    const ticket = (this.ticket = (this.ticket ?? 0) + 1);
    let chats;
    try {
      const res = await fetch(`/api/chats?${params}`);
      if (!res.ok) return;
      chats = (await res.json()).chats ?? [];
    } catch {
      return; // offline or mid-redeploy; the list is not worth an error toast
    }
    if (ticket !== this.ticket) return;
    this.chats = chats;
    this.render();
  }

  setActive(chatId) {
    this.activeId = chatId;
    for (const node of this.root.querySelectorAll('.chat-item')) {
      node.classList.toggle('active', node.dataset.id === chatId);
    }
  }

  render() {
    if (!this.chats.length) {
      const empty = this.search
        ? 'No chats match that.'
        : this.projectId
          ? 'No chats in this project yet.'
          : 'No chats yet.';
      this.root.replaceChildren(el('p', 'chat-empty', empty));
      return;
    }

    const groups = new Map();
    for (const chat of this.chats) {
      const key = bucket(chat.updated_at || chat.created_at);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(chat);
    }

    const nodes = [];
    for (const [label, chats] of groups) {
      nodes.push(el('h3', 'chat-group', label));
      for (const chat of chats) nodes.push(this.#item(chat));
    }
    this.root.replaceChildren(...nodes);
  }

  #item(chat) {
    const row = el('div', 'chat-item');
    row.dataset.id = chat.id;
    if (chat.id === this.activeId) row.classList.add('active');

    // Drag onto a project row to file it (`ProjectList` holds the drop half).
    // The plain-text copy is there so dropping a chat outside the app pastes
    // its title rather than nothing.
    row.draggable = true;
    row.addEventListener('dragstart', (event) => {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData(CHAT_MIME, chat.id);
      event.dataTransfer.setData('text/plain', chat.title);
      row.classList.add('dragging');
    });
    row.addEventListener('dragend', () => row.classList.remove('dragging'));

    const drop = async () => {
      const ok = await confirmDialog({
        title: `Delete “${chat.title}”?`,
        body: 'This cannot be undone.',
        confirm: 'Delete chat',
      });
      if (!ok) return;
      const res = await fetch(`/api/chats/${chat.id}`, { method: 'DELETE' });
      if (res.ok) {
        this.chats = this.chats.filter((c) => c.id !== chat.id);
        this.render();
        this.onDelete?.(chat.id);
      }
    };

    const open = el('button', 'chat-open');
    open.type = 'button';
    open.title = chat.title;
    open.append(el('span', 'chat-title', chat.title));
    open.addEventListener('click', () => this.onOpen?.(chat.id));
    // Same as a project row, and as every list of files: Delete removes the
    // row you are standing on. The button beside it is the pointer's way in.
    open.addEventListener('keydown', (event) => {
      if (!DELETE_KEYS.has(event.key)) return;
      event.preventDefault();
      drop();
    });

    const remove = el('button', 'chat-delete');
    remove.type = 'button';
    // A `title` rather than the app's own tooltip: this row lives in a
    // scroller, which would clip one drawn beside it.
    remove.title = `Delete chat · ${label('delete')}`;
    remove.setAttribute('aria-label', `Delete ${chat.title}`);
    remove.innerHTML = icon('trash', { size: 14 });
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      drop();
    });

    const picker = projectPicker(chat, this.projects, (projectId) => {
      chat.project_id = projectId;
      // Moved out of the project being shown, so it leaves the list it is in.
      if (this.projectId && projectId !== this.projectId) {
        this.chats = this.chats.filter((c) => c.id !== chat.id);
        this.render();
      }
      this.onMove?.(chat.id, projectId);
    });

    row.append(open, picker, remove);
    return row;
  }
}
