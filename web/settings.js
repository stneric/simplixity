// Settings live on the server, not in localStorage: the backend needs them as
// defaults for any request that omits a model, and a single-user install should
// not lose its configuration to a cleared browser cache.
//
// Writes are debounced and last-write-wins.

import { ModelPicker } from './picker.js';
import { iconNode } from './icons.js';
import { loadDepths } from './depth.js';
import { loadLenses } from './lenses.js';

const DEBOUNCE_MS = 400;

export class Settings {
  constructor({ onChange } = {}) {
    this.values = null;
    this.onChange = onChange;
    this.timer = null;
    this.statusEl = document.getElementById('settings-status');
  }

  async init() {
    this.values = await fetch('/api/settings').then((r) => r.json());

    // A list, so a second picker can be added without touching `#syncChat`
    // or `#loadAll`.
    this.chatPickers = ['chat-model', 'settings-chat-model']
      .map((id) => document.getElementById(id))
      .filter(Boolean)
      .map(
        (node) =>
          new ModelPicker(node, 'chat', (sel) => {
            this.patch({ chat_provider: sel.provider, chat_model: sel.id });
            this.#syncChat();
          }),
      );

    this.embedding = new ModelPicker(
      document.getElementById('embedding-model'),
      'embedding',
      (sel) => this.patch({ embedding_provider: sel.provider, embedding_model: sel.id }),
    );

    // The router picks from the chat list: it is a chat model, only a smaller
    // one, and it answers the same structured call.
    this.router = new ModelPicker(document.getElementById('router-model'), 'chat', (sel) =>
      this.patch({ router_provider: sel.provider, router_model: sel.id }),
    );

    // A chat model too, ruling on citations; best one of another family.
    this.judge = new ModelPicker(document.getElementById('judge-model'), 'chat', (sel) =>
      this.patch({ judge_provider: sel.provider, judge_model: sel.id }),
    );

    // Also a chat model, and the one call in the app where the cheapest model
    // in the install is unambiguously the right choice.
    this.title = new ModelPicker(document.getElementById('title-model'), 'chat', (sel) =>
      this.patch({ title_provider: sel.provider, title_model: sel.id }),
    );

    // Paint the saved choice immediately, then reconcile once the catalogue
    // arrives. The picker must never look empty on load.
    this.#syncAll();

    this.#initTabs();
    await this.#initDepth();
    await this.#initLenses();
    this.#initEmbeddingToggle();
    this.#initRouterToggle();
    this.#initJudgeToggle();
    this.#initTitleToggle();
    this.#initHomeLocation();

    document.getElementById('refresh-models').addEventListener('click', async () => {
      this.#status('refreshing…');
      await this.#loadAll({ refresh: true });
      this.#status('model list refreshed');
    });

    await this.#loadAll();
    this.#status('');
  }

  /** The tab strip. Roving tabindex and arrow keys, because a tablist that can
   *  only be operated with a mouse is a row of buttons wearing the ARIA of one.
   *
   *  Nothing is unmounted when a panel hides: the pickers keep their state and
   *  their listeners, and a `hidden` panel costs one attribute. */
  #initTabs() {
    const tabs = [...document.querySelectorAll('.tabstrip .tab')];
    if (!tabs.length) return;

    const select = (tab, { focus = true } = {}) => {
      for (const other of tabs) {
        const on = other === tab;
        other.setAttribute('aria-selected', String(on));
        other.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(other.getAttribute('aria-controls'));
        if (panel) panel.hidden = !on;
      }
      if (focus) tab.focus();
      // A tab switched while the previous panel was scrolled leaves the new one
      // showing its middle.
      document.querySelector('.modal-body')?.scrollTo?.(0, 0);
    };

    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => select(tab, { focus: false }));
      tab.addEventListener('keydown', (event) => {
        const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
        if (!step) return;
        event.preventDefault();
        select(tabs[(index + step + tabs.length) % tabs.length]);
      });
    });
  }

  async #initDepth() {
    this.depthEl = document.getElementById('depth');
    const depths = await loadDepths();
    this.depthEl.replaceChildren(
      ...depths.map((depth) => {
        const option = document.createElement('option');
        option.value = depth.id;
        option.textContent = `${depth.label}: ${depth.description}`;
        return option;
      }),
    );
    this.depthEl.value = this.values.depth;
    this.depthEl.addEventListener('change', () => this.patch({ depth: this.depthEl.value }));
  }

  /** The modal's copy of the source selector: the same toggles as the composer
   *  chip, laid out to be read rather than to fit in a chip. */
  async #initLenses() {
    const host = document.getElementById('settings-lenses');
    if (!host) return;
    this.lensCatalog = await loadLenses();
    this.#renderLenses(host);
  }

  #renderLenses(host) {
    host.replaceChildren(
      ...this.lensCatalog.map((lens) => {
        const on = (this.values.lenses ?? []).includes(lens.id);
        const row = document.createElement('button');
        row.type = 'button';
        row.className = `lens-toggle${on ? ' on' : ''}`;
        row.setAttribute('aria-pressed', String(on));

        const text = document.createElement('span');
        text.className = 'lens-toggle-text';
        const name = document.createElement('span');
        name.className = 'lens-toggle-label';
        name.textContent = lens.label;
        const desc = document.createElement('span');
        desc.className = 'lens-toggle-desc';
        desc.textContent = lens.description;
        text.append(name, desc);

        const mark = document.createElement('span');
        mark.className = 'lens-toggle-mark';
        mark.append(iconNode('check', 14));

        row.append(text, mark);
        row.addEventListener('click', () => {
          const current = this.values.lenses ?? [];
          const next = on ? current.filter((id) => id !== lens.id) : [...current, lens.id];
          if (!next.length) return; // never zero: a run has to search something
          this.patch({ lenses: this.lensCatalog.map((l) => l.id).filter((id) => next.includes(id)) });
          this.#renderLenses(host);
        });
        return row;
      }),
    );
  }

  #initEmbeddingToggle() {
    this.embedToggle = document.getElementById('embedding-enabled');
    this.embedToggle.checked = this.values.embedding_enabled;
    this.embedToggle.addEventListener('change', () => {
      this.patch({ embedding_enabled: this.embedToggle.checked });
      this.#reflectEmbeddingState();
    });
    this.#reflectEmbeddingState();
  }

  /** On means a separate router model is in use, which is the way round every
   *  other switch in this app reads.
   *
   *  Off clears the stored id, because a setting nothing reads is a
   *  setting that lies, but the choice is kept for the session so flipping the
   *  switch twice does not cost the pick. */
  #initRouterToggle() {
    this.routerOn = document.getElementById('router-on');
    if (!this.routerOn) return;
    this.lastRouter = this.values.router_model
      ? { provider: this.values.router_provider, id: this.values.router_model }
      : null;
    this.routerOn.checked = Boolean(this.values.router_model);
    this.routerOn.addEventListener('change', () => {
      if (!this.routerOn.checked) {
        if (this.values.router_model) {
          this.lastRouter = {
            provider: this.values.router_provider,
            id: this.values.router_model,
          };
        }
        this.patch({ router_model: '', router_provider: '' });
      } else {
        // Back to what was chosen before, or to the chat provider with nothing
        // selected yet, which is what the picker below is for.
        this.patch({
          router_provider: this.lastRouter?.provider || this.values.chat_provider,
          router_model: this.lastRouter?.id || '',
        });
      }
      this.#reflectRouterState();
    });
    this.#reflectRouterState();
  }

  /** Same contract as the router's switch: on means a judge of its own, off
   *  clears the stored id and keeps the pick for the session. */
  #initJudgeToggle() {
    this.judgeOn = document.getElementById('judge-on');
    if (!this.judgeOn) return;
    this.lastJudge = this.values.judge_model
      ? { provider: this.values.judge_provider, id: this.values.judge_model }
      : null;
    this.judgeOn.checked = Boolean(this.values.judge_model);
    this.judgeOn.addEventListener('change', () => {
      if (!this.judgeOn.checked) {
        if (this.values.judge_model) {
          this.lastJudge = { provider: this.values.judge_provider, id: this.values.judge_model };
        }
        this.patch({ judge_model: '', judge_provider: '' });
      } else {
        this.patch({
          judge_provider: this.lastJudge?.provider || this.values.chat_provider,
          judge_model: this.lastJudge?.id || '',
        });
      }
      this.#reflectJudgeState();
    });
    this.#reflectJudgeState();
  }

  #reflectJudgeState() {
    const on = this.judgeOn.checked;
    document.getElementById('judge-model')?.classList.toggle('disabled', !on);
    this.#switchText('judge-state', on);
  }

  #reflectRouterState() {
    const on = this.routerOn.checked;
    document.getElementById('router-model')?.classList.toggle('disabled', !on);
    this.#switchText('router-state', on);
  }

  /** Whether a new chat gets a model-written name at all.
   *
   *  A flag of its own rather than the router's trick of clearing the model id:
   *  the two are separate questions here, "should chats be named" and "by
   *  which model", and off has to survive a model being picked afterwards.
   *  Off leaves the chat named after its first line, which is what every chat
   *  is named until this call comes back anyway. */
  #initTitleToggle() {
    this.titleOn = document.getElementById('title-on');
    if (!this.titleOn) return;
    this.titleOn.checked = Boolean(this.values.title_enabled);
    this.titleOn.addEventListener('change', () => {
      this.patch({ title_enabled: this.titleOn.checked });
      this.#reflectTitleState();
    });
    this.#reflectTitleState();
  }

  /** What will actually name a chat, which is not always what is stored.
   *
   *  Nothing chosen means the title call rides on the router, and on the chat
   *  model when there is no router either. Mirrors `runs._title_spec`; the
   *  picker has to show what will run, not a blank where a model should be.
   *  Note that `title_provider` is only meaningful alongside `title_model`, it
   *  carries a default of its own and would otherwise win over both fallbacks. */
  #titleSelection() {
    const v = this.values;
    if (v.title_model) {
      return { provider: v.title_provider || v.chat_provider, model: v.title_model };
    }
    if (v.router_model) {
      return { provider: v.router_provider || v.chat_provider, model: v.router_model };
    }
    return { provider: v.chat_provider, model: v.chat_model };
  }

  #reflectTitleState() {
    const on = this.titleOn.checked;
    document.getElementById('title-model')?.classList.toggle('disabled', !on);
    this.#switchText('title-state', on);
  }

  /** The word next to a switch. Without it the two toggles in this modal are
   *  indistinguishable, and they do not mean the same thing. */
  #switchText(id, on) {
    const node = document.getElementById(id);
    if (node) node.textContent = on ? 'On' : 'Off';
  }

  /** A bare place name: the weather engines hand the whole string to a
   *  geocoder, which resolves "Berlin" and raises on "weather in Berlin". */
  #initHomeLocation() {
    this.homeEl = document.getElementById('home-location');
    if (!this.homeEl) return;
    this.homeEl.value = this.values.home_location ?? '';
    this.homeEl.addEventListener('input', () =>
      this.patch({ home_location: this.homeEl.value.trim() }),
    );
  }

  async #loadAll({ refresh = false } = {}) {
    await Promise.all([
      ...this.chatPickers.map((p) => p.load({ refresh })),
      this.embedding.load({ refresh }),
      this.router.load({ refresh }),
      this.judge.load({ refresh }),
      this.title.load({ refresh }),
    ]);
    this.#syncAll();
  }

  #syncChat() {
    for (const picker of this.chatPickers) {
      picker.setSelection(this.values.chat_provider, this.values.chat_model);
    }
  }

  #syncAll() {
    this.#syncChat();
    this.embedding.setSelection(this.values.embedding_provider, this.values.embedding_model);
    // Empty means "the chat model triages too", so the picker shows what will
    // actually run rather than a blank where a model should be.
    this.router?.setSelection(
      this.values.router_provider || this.values.chat_provider,
      this.values.router_model || this.values.chat_model,
    );
    // Empty shows nothing chosen: unlike the router there is no single model
    // standing in, the stand-in depends on the project (`RunContext.checker`).
    this.judge?.setSelection(this.values.judge_provider, this.values.judge_model);
    const title = this.#titleSelection();
    this.title?.setSelection(title.provider, title.model);
    if (this.depthEl) this.depthEl.value = this.values.depth;
    if (this.homeEl && this.homeEl !== document.activeElement) {
      this.homeEl.value = this.values.home_location ?? '';
    }
  }

  #reflectEmbeddingState() {
    const on = this.embedToggle.checked;
    document.getElementById('embedding-model').classList.toggle('disabled', !on);
    this.#switchText('embedding-state', on);
  }

  /** Repaint every picker from the saved values, for a change made elsewhere:
   *  the project bar edits the chat model too (project-view.js). */
  refresh() {
    this.#syncAll();
  }

  patch(delta) {
    this.values = { ...this.values, ...delta };
    if (this.depthEl && 'depth' in delta) this.depthEl.value = delta.depth;
    // A model picked from the slot below turns the switch on by itself: the two
    // controls edit one setting, and a picker that works while the switch reads
    // "Off" is the confusion this whole control had.
    if (this.routerOn && 'router_model' in delta && delta.router_model) {
      this.routerOn.checked = true;
      this.#reflectRouterState();
    }
    // Same courtesy for titles: choosing the model that would do the naming is
    // an odd way to say "do not name anything".
    if (this.judgeOn && 'judge_model' in delta && delta.judge_model) {
      this.judgeOn.checked = true;
      this.#reflectJudgeState();
    }
    if (this.titleOn && 'title_model' in delta && delta.title_model) {
      this.values.title_enabled = true;
      this.titleOn.checked = true;
      this.#reflectTitleState();
    }
    // The composer chip and the modal edit the same setting from two places, so
    // whichever one did not originate the change is repainted.
    const lensHost = document.getElementById('settings-lenses');
    if (lensHost && this.lensCatalog && 'lenses' in delta) this.#renderLenses(lensHost);
    this.onChange?.(this.values);
    this.#status('saving…');
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.#flush(), DEBOUNCE_MS);
  }

  async #flush() {
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(this.values),
    }).catch(() => null);

    if (!res?.ok) {
      this.#status('could not save', true);
      return;
    }
    this.values = await res.json();
    this.#status('saved');
    setTimeout(() => this.#status(''), 1500);
  }

  #status(text, isError = false) {
    if (!this.statusEl) return;
    this.statusEl.textContent = text;
    this.statusEl.classList.toggle('error', isError);
  }
}
