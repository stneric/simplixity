// Being inside a project: the bar across the top that says so, and the page a
// project opens on.
//
// Selecting a project used to change three things nobody could see at once: a
// highlighted row in the sidebar, a filtered chat list, and a placeholder in the
// composer. The way back out was clicking the highlighted row again, which is a
// folder's gesture with no folder's affordance. So a project now announces
// itself where every page announces where you are, at the top: its name, which
// leads back to its page, and a close button that leaves it.
//
// The same bar carries the model, and only here. An ordinary chat stays as
// plain as it was (the model is in Settings, see index.html); a project is
// where the choice matters per place, a literature project above all, whose
// answers are written and judged by the literature model, not the chat model.
// The picker says which of the two it sets.
//
// The page a project opens on, when no chat of it is open, is what the welcome
// screen is to the app: what this place is, what it holds, and the way into
// its reading lists.

import { iconNode } from './icons.js';
import { ModelPicker } from './picker.js';
import { isLibrary } from './source-switch.js';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** Where a reading list opens. The literature page routes on its hash. */
export const listUrl = (id) => `/literature#list=${encodeURIComponent(id)}`;

function button(label, className, icon) {
  const node = el('button', className);
  node.type = 'button';
  if (icon) node.append(iconNode(icon, 15));
  node.append(el('span', '', label));
  return node;
}

export class ProjectBar {
  /**
   * @param {HTMLElement} root  the slot in the topbar
   * @param {object} hooks
   * @param {() => void} hooks.onHome   the project's name was clicked
   * @param {() => void} hooks.onLeave  the close button was clicked
   * @param {import('./settings.js').Settings} hooks.settings
   */
  constructor(root, { onHome, onLeave, settings }) {
    this.root = root;
    this.settings = settings;
    this.project = null;

    this.crumb = el('button', 'project-crumb');
    this.crumb.type = 'button';
    this.name = el('span', 'project-crumb-name');
    this.crumb.append(iconNode('folder', 14), this.name);
    this.crumb.addEventListener('click', () => onHome?.());

    this.leave = el('button', 'icon-btn small project-leave');
    this.leave.type = 'button';
    this.leave.append(iconNode('close', 14));
    this.leave.addEventListener('click', () => onLeave?.());

    // One picker, rebuilt never: it listens on the document, and a picker per
    // project switch would leave a listener behind for each (literature.js
    // learned the same).
    this.label = el('span', 'project-model-label', 'Model');
    this.slot = el('div', 'project-model');
    this.note = el('span', 'project-model-note');
    this.picker = new ModelPicker(this.slot, 'chat', (selection) => this.#save(selection));
    const model = el('div', 'project-model-wrap');
    model.append(this.label, this.slot, this.note);

    const where = el('div', 'project-where');
    where.append(this.crumb, this.leave);
    this.root.append(where, model);
    this.loaded = false;
  }

  /** Show the bar for this project, or hide it for none. */
  set(project) {
    this.project = project ?? null;
    this.root.hidden = !project;
    if (!project) return;
    this.name.textContent = project.name;
    this.crumb.title = `${project.name}: the project’s page`;
    this.leave.title = `Leave ${project.name}`;
    this.leave.setAttribute('aria-label', `Leave the project ${project.name}`);
    if (!this.loaded) {
      this.loaded = true;
      this.picker.load();
    }
    this.sync();
  }

  /** Repaint the picker from the saved settings. */
  sync() {
    const s = this.settings.values ?? {};
    if (!this.project) return;
    if (isLibrary(this.project)) {
      // Empty means the chat model writes these answers too, and the picker
      // shows that model rather than a blank, with a note saying it is shared.
      const own = Boolean(s.literature_model);
      this.picker.setSelection(
        own ? s.literature_provider || s.chat_provider : s.chat_provider,
        own ? s.literature_model : s.chat_model,
      );
      this.note.textContent = own ? '' : 'same as chat';
      this.slot.title =
        'The literature model: writes and checks every answer in projects with a reading list, ' +
        'and talks and screens on the Literature page. The same setting there.';
    } else {
      this.picker.setSelection(s.chat_provider, s.chat_model);
      this.note.textContent = '';
      this.slot.title = 'The chat model, the same one Settings sets. It writes every ordinary answer.';
    }
  }

  #save(selection) {
    if (this.project && isLibrary(this.project)) {
      this.settings.patch({ literature_provider: selection.provider, literature_model: selection.id });
    } else {
      this.settings.patch({ chat_provider: selection.provider, chat_model: selection.id });
    }
    this.settings.refresh();
    this.sync();
  }
}

/**
 * The page a project opens on.
 *
 * Fetches what it shows itself, the chats filed here and the reading lists, so
 * it is right however the project was entered. Rendered again on every entry,
 * which is cheap and means a list made on the literature page a minute ago is
 * there when the person comes back.
 */
export class ProjectHome {
  /**
   * @param {HTMLElement} root
   * @param {object} hooks
   * @param {(chatId: string) => void} hooks.onOpenChat
   * @param {(project: object) => void} hooks.onRename
   * @param {() => void} hooks.onLeave
   * @param {() => void} hooks.onAsk   focus the composer
   */
  constructor(root, hooks) {
    this.root = root;
    this.hooks = hooks;
    this.token = 0;
  }

  hide() {
    this.root.hidden = true;
    this.token += 1;
  }

  async show(project) {
    const token = ++this.token;
    this.root.hidden = false;
    this.#render(project, null, null);
    const [chats, lists] = await Promise.all([
      fetch(`/api/chats?project_id=${encodeURIComponent(project.id)}&limit=6`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => data?.chats ?? [])
        .catch(() => []),
      fetch('/api/literature')
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => (data?.collections ?? []).filter((c) => c.project_id === project.id))
        .catch(() => []),
    ]);
    // Left before the answers came back, or entered another project since.
    if (token !== this.token) return;
    this.#render(project, chats, lists);
  }

  #render(project, chats, lists) {
    const library = isLibrary(project);
    const head = el('header', 'project-home-head');
    const title = el('div', 'project-home-title');
    title.append(iconNode(library ? 'library' : 'folder', 20), el('h1', '', project.name));
    title.append(el('span', `project-kind${library ? ' library' : ''}`, library ? 'Literature project' : 'Project'));
    const actions = el('div', 'project-home-actions');
    const rename = button('Rename', 'ghost-btn', 'settings');
    rename.addEventListener('click', () => this.hooks.onRename?.(project));
    const leave = button('Leave project', 'ghost-btn', 'back');
    leave.addEventListener('click', () => this.hooks.onLeave?.());
    actions.append(rename, leave);
    head.append(title, actions);

    const lede = el(
      'p',
      'project-home-lede',
      library
        ? 'Answers here come only from the papers marked relevant in this project’s reading lists, ' +
            'cited by page and checked against it. Ask about the whole collection, an introduction ' +
            'or an overview, and every paper’s abstract is read too. Switch the composer to Web to ' +
            'look beyond the papers, or on Chats to build on earlier answers here.'
        : 'A folder for chats, and a corpus: switch on Chats in the composer and what you have ' +
            'already answered here is searched alongside the web. File a reading list under it and ' +
            'its chats answer from your papers instead.',
    );

    const stats = el('dl', 'project-stats');
    const stat = (value, label) => {
      const cell = el('div', 'project-stat');
      cell.append(el('dt', '', label), el('dd', '', String(value ?? 0)));
      stats.append(cell);
    };
    stat(project.chats, 'Chats');
    stat(project.lists, 'Reading lists');
    stat(project.relevant, 'Relevant papers');
    stat(project.fulltexts, 'With full text');

    const nodes = [head, lede, stats];

    if (library && Number(project.relevant) > 0 && Number(project.fulltexts) < Number(project.relevant)) {
      // The one gap that changes answers without changing anything visible: a
      // relevant paper without its PDF is searched as its abstract alone.
      const missing = Number(project.relevant) - Number(project.fulltexts);
      nodes.push(
        el(
          'p',
          'project-warn',
          `${plural(missing, 'relevant paper')} ${missing === 1 ? 'has' : 'have'} no full text yet, ` +
            `so ${missing === 1 ? 'it is' : 'they are'} searched as an abstract only. ` +
            'Upload the PDFs on the reading list’s Full texts tab.',
        ),
      );
    }

    nodes.push(this.#lists(project, lists), this.#chats(chats));
    this.root.replaceChildren(...nodes);
  }

  #lists(project, lists) {
    const section = el('section', 'project-section');
    const head = el('div', 'project-section-head');
    head.append(el('h2', '', 'Reading lists'));
    const add = button('New reading list', 'ghost-btn', 'plus');
    add.addEventListener('click', async () => {
      add.disabled = true;
      const res = await fetch('/api/literature', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: '', project_id: project.id }),
      }).catch(() => null);
      if (res?.ok) location.assign(listUrl((await res.json()).id));
      else add.disabled = false;
    });
    const all = el('a', 'ghost-btn', null);
    all.href = '/literature';
    all.append(iconNode('library', 15), el('span', '', 'Open Literature'));
    head.append(el('span', 'tool-spacer'), all, add);
    section.append(head);

    if (lists === null) {
      section.append(el('p', 'project-empty', 'Loading…'));
      return section;
    }
    if (!lists.length) {
      section.append(
        el(
          'p',
          'project-empty',
          'None yet. A reading list finds papers for a question, screens them with you and holds ' +
            'their PDFs; filed here, it makes this a literature project.',
        ),
      );
      return section;
    }
    const list = el('div', 'project-rows');
    for (const c of lists) {
      const row = el('a', 'project-row');
      row.href = listUrl(c.id);
      const text = el('span', 'project-row-text');
      text.append(el('span', 'project-row-name', c.name || 'Untitled reading list'));
      const parts = [`${c.relevant ?? 0} relevant`, `${c.total ?? 0} papers`];
      if (c.open) parts.push(`${c.open} to screen`);
      text.append(el('span', 'project-row-meta', parts.join(' · ')));
      row.append(iconNode('book', 16), text, iconNode('chevron', 14));
      list.append(row);
    }
    section.append(list);
    return section;
  }

  #chats(chats) {
    const section = el('section', 'project-section');
    const head = el('div', 'project-section-head');
    head.append(el('h2', '', 'Recent chats'));
    section.append(head);
    if (chats === null) {
      section.append(el('p', 'project-empty', 'Loading…'));
      return section;
    }
    if (!chats.length) {
      const ask = button('Ask the first question', 'ghost-btn', 'message');
      ask.addEventListener('click', () => this.hooks.onAsk?.());
      const empty = el('p', 'project-empty', 'No chats in this project yet. ');
      section.append(empty, ask);
      return section;
    }
    const list = el('div', 'project-rows');
    for (const chat of chats) {
      const row = el('button', 'project-row');
      row.type = 'button';
      const text = el('span', 'project-row-text');
      text.append(el('span', 'project-row-name', chat.title || 'Untitled chat'));
      row.append(iconNode('message', 16), text, iconNode('chevron', 14));
      row.addEventListener('click', () => this.hooks.onOpenChat?.(chat.id));
      list.append(row);
    }
    section.append(list);
    return section;
  }
}
