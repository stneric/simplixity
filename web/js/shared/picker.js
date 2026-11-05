// Tabbed model picker.
//
// A native <select> with <optgroup> was the cheap option, but it cannot do
// tabs, search, or per-row metadata, and with 400+ OpenRouter models an
// unsearchable list is unusable. So this is a small custom combobox.
//
// Free is the default landing tab, deliberately, unless the person has starred
// models: then Favourites, the star before it, is where they land, since a
// list they made themselves is the one they came for.
//
// Tabs are an icon and a name, and only the icon on a phone (styles.css), where
// four names do not fit beside each other. Favourites is only ever its star:
// outlined while another tab is open, filled while it is the open one.

import { icon } from './icons.js';

const TABS = [
  { id: 'favorites', label: 'Favourites', icon: 'star', iconOnly: true },
  { id: 'free', label: 'Free', icon: 'tag' },
  { id: 'local', label: 'Local', icon: 'laptop' },
  { id: 'all', label: 'All', icon: 'layers' },
];

// ── favourites ───────────────────────────────────────────────────────
//
// One list for every picker on the page (the settings modal alone has five),
// loaded once and saved whole on each change. Server-side, per profile, in a
// row of its own (settings_store.Favorites), so no other save can undo a star.

const favoriteKey = (provider, id) => `${provider}\u0000${id}`;

const favorites = {
  list: [],
  keys: new Set(),
  loading: null,
  listeners: new Set(),
};

function loadFavorites() {
  favorites.loading ??= fetch('/api/settings/favorites')
    .then((res) => (res.ok ? res.json() : { models: [] }))
    .catch(() => ({ models: [] }))
    .then((data) => setFavorites(data.models ?? []));
  return favorites.loading;
}

function setFavorites(list) {
  favorites.list = list;
  favorites.keys = new Set(list.map((m) => favoriteKey(m.provider, m.id)));
  for (const listener of favorites.listeners) listener();
}

async function toggleFavorite(model) {
  const key = favoriteKey(model.provider, model.id);
  const before = favorites.list;
  // Shown at once and undone if the save fails: a star that waits for the
  // network feels broken, one that silently stays lit is worse.
  setFavorites(
    favorites.keys.has(key)
      ? before.filter((m) => favoriteKey(m.provider, m.id) !== key)
      : [...before, { provider: model.provider, id: model.id }],
  );
  const res = await fetch('/api/settings/favorites', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ models: favorites.list }),
  }).catch(() => null);
  if (!res?.ok) setFavorites(before);
}

const priceLabel = (model) => {
  if (model.local) return 'local';
  if (model.free) return 'free';
  if (model.prompt_price == null) return '';
  // Prices are per token; per-million is the unit people actually think in.
  const perMillion = model.prompt_price * 1e6;
  return perMillion >= 1 ? `$${perMillion.toFixed(2)}/M` : `$${perMillion.toFixed(3)}/M`;
};

const contextLabel = (model) =>
  model.context_length ? `${Math.round(model.context_length / 1000)}k` : '';

export class ModelPicker {
  /**
   * @param {HTMLElement} root       container
   * @param {string} kind            'chat' | 'embedding'
   * @param {(sel:{provider:string,id:string}) => void} onSelect
   */
  constructor(root, kind, onSelect) {
    this.root = root;
    this.kind = kind;
    this.onSelect = onSelect;
    this.groups = { free: [], local: [], all: [] };
    this.problems = [];
    this.tab = 'favorites';
    this.query = '';
    this.selected = null;
    this.open = false;
    this.#build();
    document.addEventListener('click', (e) => {
      if (!this.root.contains(e.target)) this.#setOpen(false);
    });
    favorites.listeners.add(() => {
      this.#renderTabs();
      if (this.open) this.#renderList();
    });
    loadFavorites();
  }

  /** The models in a tab. Favourites are the starred ones the catalogue still
   *  has, in the order they were starred. */
  #models(tab) {
    if (tab !== 'favorites') return this.groups[tab] ?? [];
    return favorites.list
      .map((fav) => this.#find(fav.provider, fav.id))
      .filter(Boolean);
  }

  /** The first tab with something in it, favourites first. */
  #landing() {
    return TABS.map((t) => t.id).find((id) => this.#models(id).length) ?? 'all';
  }

  #build() {
    this.root.classList.add('picker');
    this.root.innerHTML = `
      <button type="button" class="picker-trigger" aria-haspopup="listbox" aria-expanded="false">
        <span class="picker-value">Loading…</span>
        <span class="picker-caret" aria-hidden="true">▾</span>
      </button>
      <div class="picker-panel" hidden>
        <div class="picker-tabs" role="tablist">
          ${TABS.map(
            (t) =>
              `<button type="button" class="picker-tab${t.iconOnly ? ' icon-only' : ''}" role="tab" data-tab="${t.id}" title="${t.label}" aria-label="${t.label}">${icon(t.icon, { size: 15 })}${t.iconOnly ? '' : `<span class="picker-tab-label">${t.label}</span>`}<span class="count"></span></button>`,
          ).join('')}
        </div>
        <input type="search" class="picker-search" placeholder="Filter models…" />
        <div class="picker-list" role="listbox"></div>
        <p class="picker-problems" hidden></p>
      </div>`;

    this.trigger = this.root.querySelector('.picker-trigger');
    this.panel = this.root.querySelector('.picker-panel');
    this.list = this.root.querySelector('.picker-list');
    this.search = this.root.querySelector('.picker-search');
    this.problemsEl = this.root.querySelector('.picker-problems');

    this.trigger.addEventListener('click', () => this.#setOpen(!this.open));
    this.search.addEventListener('input', () => {
      this.query = this.search.value.trim().toLowerCase();
      this.#renderList();
    });
    this.root.querySelectorAll('.picker-tab').forEach((tab) =>
      tab.addEventListener('click', () => {
        this.tab = tab.dataset.tab;
        this.#renderTabs();
        this.#renderList();
      }),
    );
    this.root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.open) {
        this.#setOpen(false);
        this.trigger.focus();
      }
    });
  }

  #setOpen(open) {
    this.open = open;
    this.panel.hidden = !open;
    this.trigger.setAttribute('aria-expanded', String(open));
    if (open) {
      // Land on a tab that actually has something in it.
      if (!this.#models(this.tab).length) this.tab = this.#landing();
      this.#renderTabs();
      this.#renderList();
      this.search.focus();
    }
  }

  async load({ refresh = false } = {}) {
    const params = new URLSearchParams({ kind: this.kind });
    if (refresh) params.set('refresh', 'true');
    const res = await fetch(`/api/models?${params}`);
    if (!res.ok) {
      this.#setLabel('Discovery failed');
      return;
    }
    const data = await res.json();
    this.groups = data.groups ?? { free: [], local: [], all: [] };
    this.problems = data.problems ?? [];
    // Opened before the catalogue arrived: the panel landed on an empty tab
    // and said "No models here", and nothing redrew it once there were some.
    if (this.open && !this.#models(this.tab).length) this.tab = this.#landing();
    this.#renderTabs();
    this.#renderProblems();
    this.#renderLabel();
    if (this.open) this.#renderList();
  }

  /** Select by persisted identity, even before the catalogue has loaded. */
  setSelection(provider, id) {
    this.selected = provider && id ? { provider, id } : null;
    this.#renderLabel();
  }

  /** The catalogue entry of the selected model, or null before the catalogue
   *  has arrived or when the model is no longer in it. */
  get current() {
    return this.selected ? (this.#find(this.selected.provider, this.selected.id) ?? null) : null;
  }

  #find(provider, id) {
    return this.groups.all.find((m) => m.provider === provider && m.id === id);
  }

  #renderLabel() {
    if (!this.selected) {
      this.#setLabel('Select a model');
      return;
    }
    const known = this.#find(this.selected.provider, this.selected.id);
    // A saved model can outlive its availability. Say so rather than
    // silently showing a model that is no longer reachable.
    this.#setLabel(known ? known.name : `${this.selected.id} (unavailable)`, !known);
  }

  #setLabel(text, warn = false) {
    const el = this.root.querySelector('.picker-value');
    el.textContent = text;
    el.classList.toggle('warn', warn);
  }

  #renderTabs() {
    this.root.querySelectorAll('.picker-tab').forEach((tab) => {
      const count = this.#models(tab.dataset.tab).length;
      const active = tab.dataset.tab === this.tab;
      // The star says nothing but itself; a number beside it would crowd it.
      tab.querySelector('.count').textContent = count && !tab.classList.contains('icon-only') ? ` ${count}` : '';
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      // Favourites stays clickable while empty: it is where the person learns
      // that the stars beside each model fill it.
      tab.disabled = count === 0 && tab.dataset.tab !== 'favorites';
    });
  }

  #renderProblems() {
    if (!this.problems.length) {
      this.problemsEl.hidden = true;
      return;
    }
    this.problemsEl.hidden = false;
    this.problemsEl.textContent = this.problems
      .map((p) => `${p.provider}: ${p.error}`)
      .join(' · ');
  }

  #renderList() {
    const models = this.#models(this.tab).filter(
      (m) => !this.query || m.name.toLowerCase().includes(this.query) || m.id.toLowerCase().includes(this.query),
    );

    if (!models.length) {
      const empty = document.createElement('p');
      empty.className = 'picker-empty';
      empty.textContent =
        this.tab === 'favorites' && !this.query
          ? 'No favourites yet. Star a model in another tab to keep it here.'
          : `No models here${this.query ? ' for that filter' : ''}.`;
      this.list.replaceChildren(empty);
      return;
    }

    this.list.replaceChildren(
      ...models.map((model) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'picker-row';
        row.role = 'option';
        const isSelected =
          this.selected?.provider === model.provider && this.selected?.id === model.id;
        row.setAttribute('aria-selected', String(isSelected));
        if (isSelected) row.classList.add('selected');

        const name = document.createElement('span');
        name.className = 'picker-row-name';
        name.textContent = model.name;

        const meta = document.createElement('span');
        meta.className = 'picker-row-meta';
        const bits = [model.provider, contextLabel(model), priceLabel(model), model.vision ? 'vision' : ''].filter(Boolean);
        meta.textContent = bits.join(' · ');

        row.append(name, meta);
        row.addEventListener('click', () => {
          this.selected = { provider: model.provider, id: model.id };
          this.#renderLabel();
          this.#setOpen(false);
          this.onSelect?.(this.selected);
        });

        // Beside the row rather than inside it: a button cannot hold another.
        const starred = favorites.keys.has(favoriteKey(model.provider, model.id));
        const star = document.createElement('button');
        star.type = 'button';
        star.className = 'picker-star';
        star.setAttribute('aria-pressed', String(starred));
        star.setAttribute('aria-label', starred ? `Remove ${model.name} from favourites` : `Add ${model.name} to favourites`);
        star.title = starred ? 'Remove from favourites' : 'Add to favourites';
        star.innerHTML = icon('star', { size: 15 });
        star.addEventListener('click', (event) => {
          // The document listener closes the panel on clicks outside it, and
          // a re-render can detach this button before that check runs.
          event.stopPropagation();
          toggleFavorite(model);
        });

        const item = document.createElement('div');
        item.className = 'picker-item';
        item.append(row, star);
        return item;
      }),
    );
  }
}
