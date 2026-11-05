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
//
// Pictures ride along in the same list, but they are not documents: they are
// not searched, they do not turn the chat into one that answers only from its
// files (`count` leaves them out), and they are shown to the model with the
// question. A picture is scaled down here before it is uploaded, and its chip
// warns when the chosen model cannot see (`setVision`).

import { iconNode } from '../shared/icons.js';
import { fileSummary, isImage, newChatId, scaledSize, unusable, uploadError } from './attachments-core.js';

/** A picture scaled down to what a model looks at, or the file itself when it
 *  is small enough or cannot be decoded here (the server then decides). A GIF
 *  is left alone: scaling it would keep only its first frame. */
async function shrink(file) {
  if (file.type === 'image/gif' || typeof createImageBitmap !== 'function') return file;
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    return file;
  }
  const size = scaledSize(bitmap.width, bitmap.height, file.size);
  if (!size) {
    bitmap.close?.();
    return file;
  }
  const canvas = document.createElement('canvas');
  canvas.width = size.width;
  canvas.height = size.height;
  canvas.getContext('2d').drawImage(bitmap, 0, 0, size.width, size.height);
  bitmap.close?.();
  // PNG keeps transparency and sharp text in a screenshot; a photo is JPEG.
  const type = file.type === 'image/png' ? 'image/png' : 'image/jpeg';
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, type, 0.88));
  if (!blob || (type === 'image/png' && blob.size > file.size)) return file;
  return new File([blob], file.name, { type });
}

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
    // Whether the chosen model sees: true, false, or null when nobody says.
    this.vision = null;
  }

  /** The chosen model changed, or its catalogue entry arrived. */
  setVision(vision) {
    if (vision === this.vision) return;
    this.vision = vision;
    this.render();
  }

  /** Files that are stored and can be asked about. */
  get ready() {
    return this.items.filter((item) => item.state === 'ready').map((item) => item.file);
  }

  /** Documents attached or on their way, sent or not: what the composer is
   *  set up for. Pictures are not counted: they do not make the chat answer
   *  only from its files. */
  get count() {
    return this.items.filter((item) => item.state !== 'error' && !item.image).length;
  }

  /** Pictures waiting to go out with the next question, stored or on their way. */
  get images() {
    return this.items.filter((item) => item.image && !item.posted && item.state !== 'error').length;
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
    this.#forget(this.items);
    this.items = (files ?? []).map((file) => ({
      key: file.id,
      name: file.name,
      state: 'ready',
      file,
      image: file.kind === 'image',
      preview: file.kind === 'image' ? file.url : null,
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
    this.#forget(this.items);
    this.chatId = null;
    this.sent = false;
    this.items = [];
    this.render();
  }

  /** Let go of the local previews of these chips. */
  #forget(items) {
    for (const item of items) if (item.local) URL.revokeObjectURL(item.local);
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
      const image = isImage(file);
      const local = image ? URL.createObjectURL(file) : null;
      const item = { key: `local-${Math.random()}`, name: file.name, state: 'uploading', file: null, image, local, preview: local };
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

  async #upload(item, original, chatId) {
    try {
      const file = item.image ? await shrink(original) : original;
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
        this.#forget([item]);
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
    this.#forget([item]);
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
      const blind = item.image && this.vision === false && item.state !== 'error';
      chip.dataset.state = blind || (item.state === 'ready' && unusable(item.file)) ? 'warn' : item.state;
      if (item.image && item.preview && item.state !== 'error') {
        chip.classList.add('image');
        const thumb = document.createElement('img');
        thumb.className = 'attachment-thumb';
        thumb.src = item.preview;
        thumb.alt = '';
        thumb.decoding = 'async';
        chip.append(thumb);
      } else {
        chip.append(iconNode(item.state === 'error' ? 'alert' : item.image ? 'image' : 'file', 15));
      }
      const text = document.createElement('span');
      text.className = 'attachment-text';
      const name = document.createElement('span');
      name.className = 'attachment-name';
      name.textContent = item.name;
      const meta = document.createElement('span');
      meta.className = 'attachment-meta';
      meta.textContent =
        item.state === 'uploading'
          ? item.image
            ? 'Uploading…'
            : 'Reading…'
          : item.state === 'error'
            ? item.error
            : blind
              ? 'This model can’t see images'
              : fileSummary(item.file);
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
