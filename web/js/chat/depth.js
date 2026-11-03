// Depth selector, used by both the composer chip and the settings modal.
//
// The presets come from /api/depths (the same definitions the agent runs on),
// so the descriptions here cannot drift away from what the backend does.

import { icon } from '../shared/icons.js';

const ICONS = { auto: 'spark', quick: 'zap', balanced: 'gauge', deep: 'telescope' };

// Used only if the API is unreachable, so the composer still works offline.
const FALLBACK = [
  { id: 'auto', label: 'Auto', description: 'Matches the effort to the question.' },
  { id: 'quick', label: 'Quick', description: 'Fast, snippets only.' },
  { id: 'balanced', label: 'Balanced', description: 'Reads the best pages.' },
  { id: 'deep', label: 'Deep', description: 'Plans and researches in parallel.' },
];

let cached = null;

export function loadDepths() {
  cached ??= fetch('/api/depths')
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => data?.depths ?? FALLBACK)
    .catch(() => FALLBACK);
  return cached;
}

export class DepthChip {
  /**
   * @param {HTMLElement} root      wrapper that will hold the chip and its menu
   * @param {(id: string) => void} onSelect
   */
  constructor(root, onSelect) {
    this.root = root;
    this.onSelect = onSelect;
    this.depths = FALLBACK;
    this.value = 'auto';
    this.open = false;
    this.#build();
  }

  #build() {
    this.root.classList.add('depth-chip');

    this.trigger = document.createElement('button');
    this.trigger.type = 'button';
    this.trigger.className = 'chip';
    this.trigger.setAttribute('aria-haspopup', 'listbox');
    this.trigger.setAttribute('aria-expanded', 'false');
    this.trigger.title = 'Research depth';

    this.menu = document.createElement('div');
    this.menu.className = 'depth-menu';
    this.menu.hidden = true;

    this.trigger.addEventListener('click', () => this.#setOpen(!this.open));
    document.addEventListener('click', (event) => {
      if (!this.root.contains(event.target)) this.#setOpen(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.open) this.#setOpen(false);
    });

    this.root.append(this.trigger, this.menu);
    this.#renderTrigger();
  }

  async init(value) {
    this.depths = await loadDepths();
    this.set(value);
    this.#renderMenu();
  }

  set(value) {
    if (this.depths.some((d) => d.id === value)) this.value = value;
    this.#renderTrigger();
    this.#renderMenu();
  }

  #setOpen(open) {
    this.open = open;
    this.menu.hidden = !open;
    this.trigger.setAttribute('aria-expanded', String(open));
  }

  #current() {
    return this.depths.find((d) => d.id === this.value) ?? this.depths[0];
  }

  #renderTrigger() {
    const current = this.#current();
    this.trigger.innerHTML = icon(ICONS[current.id] ?? 'globe', { size: 15 });
    const label = document.createElement('span');
    label.textContent = current.label;
    this.trigger.append(label);
    this.trigger.dataset.depth = current.id;
  }

  #renderMenu() {
    if (!this.menu) return;
    this.menu.replaceChildren(
      ...this.depths.map((depth) => {
        const option = document.createElement('button');
        option.type = 'button';
        option.className = 'depth-option';
        option.role = 'option';
        option.setAttribute('aria-selected', String(depth.id === this.value));
        if (depth.id === this.value) option.classList.add('selected');

        const title = document.createElement('span');
        title.className = 'depth-option-label';
        title.innerHTML = icon(ICONS[depth.id] ?? 'globe', { size: 14 });
        const name = document.createElement('span');
        name.textContent = depth.label;
        title.append(name);

        const description = document.createElement('span');
        description.className = 'depth-option-desc';
        description.textContent = depth.description;

        option.append(title, description);
        option.addEventListener('click', () => {
          this.set(depth.id);
          this.#setOpen(false);
          this.onSelect?.(depth.id);
        });
        return option;
      }),
    );
  }
}
