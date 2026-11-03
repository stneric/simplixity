// The files attached to the open chat, as chips above the composer's text.
//
// A file is uploaded the moment it is attached, not with the question: reading
// a PDF and embedding its passages takes seconds, and doing it while the person
// is still typing is the difference between a send that answers and a send
// that waits. A new chat has no id yet, so the first file picks one
// (`newChatId`) and the question is then sent under it. Removing a file
// deletes it on the server; starting a new chat before sending anything
// deletes what was attached for it, since nothing could ever reach it again.
//
// Once a question goes out, the files it went with leave the composer and are
// shown in that question's bubble instead (app.js). Chips that stayed behind
// looked like files that had never been sent. They still belong to the chat
// and every later question answers from them, so `count` keeps them: the
// composer stays set up for files, only the chips are gone.

import { iconNode } from './icons.js';
import { fileSummary, newChatId, unusable, uploadError } from './attachments-core.js';

export class Attachments {
  /**
   * @param {HTMLElement} root  the list the chips go in
   * @param {{ onChange?: () => void }} hooks
   */
  constructor(root, { onChange } = {}) {
    this.root = root;
    this.onChange = onChange;
    this.chatId = null;
    this.sent = false;
    this.items = [];
    this.pending = new Set();
  }

  /** Files that are stored and can be asked about. */
  get ready() {
    return this.items.filter((item) => item.state === 'ready').map((item) => item.file);
  }

  /** Files attached or on their way, sent or not: what the composer is set
   *  up for. */
  get count() {
    return this.items.filter((item) => item.state !== 'error').length;
  }

  /** Files stored and not yet sent with a question: what the next one carries. */
  get draft() {
    return this.items.filter((item) => item.state === 'ready' && !item.posted).map((item) => item.file);
  }

  get uploading() {
    return this.items.some((item) => item.state === 'uploading');
  }

  /** An existing chat and the files stored under it. `draft` are the ones no
   *  question has gone out with yet (`turns-core.filesByMessage`); only those
   *  are shown in the composer. */
  set(chatId, files, draft = []) {
    this.chatId = chatId;
    this.sent = true;
    const waiting = new Set(draft.map((file) => file.id));
    this.items = (files ?? []).map((file) => ({
      key: file.id,
      name: file.name,
      state: 'ready',
      file,
      posted: !waiting.has(file.id),
    }));
    this.render();
  }

  /** A new chat. Files attached for a question that was never sent go. */
  reset() {
    if (this.chatId && !this.sent) {
      for (const item of this.items) {
        if (item.file) fetch(`/api/chats/${this.chatId}/files/${item.file.id}`, { method: 'DELETE' }).catch(() => {});
      }
    }
    this.chatId = null;
    this.sent = false;
    this.items = [];
    this.render();
  }

  /** The chat id the next question has to carry, or null when nothing was
   *  attached to a new chat and the server may pick one. */
  idForQuestion() {
    return this.chatId;
  }

  /** The question went out under this id with these files: they now belong
   *  to a chat and leave the composer. Refused and failed chips go too; the
   *  question went without them, and the reason was on screen until now. */
  markSent(chatId, files = []) {
    this.chatId = chatId;
    this.sent = true;
    const ids = new Set(files.map((file) => file.id));
    this.items = this.items.filter((item) => item.state !== 'error');
    for (const item of this.items) if (item.file && ids.has(item.file.id)) item.posted = true;
    this.render();
  }

  /** Resolves once every upload in flight has finished, either way. */
  settled() {
    return Promise.allSettled([...this.pending]);
  }

  /** Upload these files to `chatId`, or to a new id for a chat not yet asked. */
  add(files, chatId) {
    this.chatId = chatId ?? this.chatId ?? newChatId();
    for (const file of files) {
      const item = { key: `local-${Math.random()}`, name: file.name, state: 'uploading', file: null };
      this.items.push(item);
      const job = this.#upload(item, file, this.chatId).finally(() => this.pending.delete(job));
      this.pending.add(job);
    }
    this.render();
  }

  /** Files refused before uploading, shown with the reason on their chip. */
  refuse(names, reason) {
    for (const name of names) this.items.push({ key: `refused-${Math.random()}`, name, state: 'error', error: reason });
    this.render();
  }

  async #upload(item, file, chatId) {
    try {
      const response = await fetch(`/api/chats/${chatId}/files`, {
        method: 'POST',
        headers: {
          'Content-Type': file.type || 'application/octet-stream',
          'X-Filename': encodeURIComponent(file.name),
        },
        body: file,
      });
      const body = await response.json().catch(() => null);
      if (!response.ok) throw new Error(uploadError(response.status, body?.detail));
      // The same file twice is the one already there: one chip, not two. One
      // that went out with an earlier question comes back to the composer, so
      // attaching it again visibly did something.
      const existing = this.items.find((other) => other !== item && other.file?.id === body.file.id);
      if (existing) {
        existing.posted = false;
        this.items = this.items.filter((other) => other !== item);
      } else {
        Object.assign(item, { key: body.file.id, state: 'ready', file: body.file });
      }
    } catch (error) {
      Object.assign(item, { state: 'error', error: error?.message ?? 'The upload failed.' });
    }
    // Only if the person is still in the chat it was attached to.
    if (chatId === this.chatId) this.render();
  }

  async #remove(item) {
    this.items = this.items.filter((other) => other !== item);
    this.render();
    if (item.file && this.chatId) {
      await fetch(`/api/chats/${this.chatId}/files/${item.file.id}`, { method: 'DELETE' }).catch(() => {});
    }
  }

  render() {
    this.root.replaceChildren();
    const shown = this.items.filter((item) => !item.posted);
    this.root.hidden = shown.length === 0;
    for (const item of shown) {
      const chip = document.createElement('li');
      chip.className = 'attachment';
      chip.dataset.state = item.state === 'ready' && unusable(item.file) ? 'warn' : item.state;
      chip.append(iconNode(item.state === 'error' ? 'alert' : 'file', 15));
      const text = document.createElement('span');
      text.className = 'attachment-text';
      const name = document.createElement('span');
      name.className = 'attachment-name';
      name.textContent = item.name;
      const meta = document.createElement('span');
      meta.className = 'attachment-meta';
      meta.textContent =
        item.state === 'uploading' ? 'Reading…' : item.state === 'error' ? item.error : fileSummary(item.file);
      text.append(name, meta);
      chip.title = `${item.name}\n${meta.textContent}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'attachment-remove';
      remove.setAttribute('aria-label', `Remove ${item.name}`);
      remove.title = 'Remove from this chat';
      remove.disabled = item.state === 'uploading';
      remove.append(iconNode('close', 13));
      remove.addEventListener('click', () => this.#remove(item));
      chip.append(text, remove);
      this.root.append(chip);
    }
    this.onChange?.();
  }
}
