// Source selector: which corpora a run may search.
//
// Multi-select, unlike depth: a question about a drug's evidence base wants the
// paper databases *and* the general web, and the two rank against each other
// once both are in. The list comes from /api/lenses, the same definitions the
// searches run on, so the menu cannot offer a corpus the backend dropped.
//
// The one invariant: never zero selected. An empty set is a run that searches
// nothing and then blames the model for the empty answer, so clicking the last
// remaining lens is a no-op rather than a way to break the app.

import { icon } from '../shared/icons.js';

const ICONS = {
  auto: 'spark',
  web: 'globe',
  scholar: 'flask',
  news: 'newspaper',
  docs: 'code',
  reference: 'book',
};

// Not a corpus, so it does not come from /api/lenses: it is the choice to let
// the classifier pick, and it is exclusive with everything else: "Auto plus
// Science" would be a contradiction with a checkmark on it.
const AUTO = {
  id: 'auto',
  label: 'Auto',
  description: 'Let the model pick the corpora the question actually needs.',
};

// Used only if the API is unreachable, so the composer still works offline.
const FALLBACK = [
  { id: 'web', label: 'Web', description: 'General search across several engines.' },
  { id: 'scholar', label: 'Science', description: 'Papers and preprints.' },
  { id: 'news', label: 'News', description: 'Newswires and newsrooms.' },
  { id: 'docs', label: 'Docs & code', description: 'Documentation and source.' },
  { id: 'reference', label: 'Reference', description: 'Encyclopaedias and primary texts.' },
];

let cached = null;

export function loadLenses() {
  cached ??= fetch('/api/lenses')
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => [AUTO, ...(data?.lenses ?? FALLBACK)])
    .catch(() => [AUTO, ...FALLBACK]);
  return cached;
}

export class LensChip {
  /**
   * @param {HTMLElement} root  wrapper that will hold the chip and its menu
   * @param {(ids: string[]) => void} onChange
   */
  constructor(root, onChange) {
    this.root = root;
    this.onChange = onChange;
    this.lenses = FALLBACK;
    this.value = ['web'];
    this.open = false;
    this.#build();
  }

  #build() {
    this.root.classList.add('lens-chip');

    this.trigger = document.createElement('button');
    this.trigger.type = 'button';
    this.trigger.className = 'chip';
    this.trigger.setAttribute('aria-haspopup', 'true');
    this.trigger.setAttribute('aria-expanded', 'false');
    this.trigger.title = 'Which sources to search';

    this.menu = document.createElement('div');
    this.menu.className = 'lens-menu';
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
    this.lenses = await loadLenses();
    this.set(value);
  }

  set(value) {
    const known = (value ?? []).filter((id) => this.lenses.some((l) => l.id === id));
    // Web, not `lenses[0]`: the first entry is Auto, and an unreadable
    // stored value must not silently start delegating the choice.
    const next = known.length ? known : ['web'];
    const moved = next.join(' ') !== this.value.join(' ');
    this.value = next;
    this.#renderTrigger();
    // Never repaint when the selection did not move. Every toggle echoes
    // straight back through onChange → settings.patch → set(), still inside
    // the original click event, and replaceChildren would detach the clicked
    // option mid-bubble, which the document-level outside-click handler then
    // reads as a click outside, closing the menu after every single pick.
    if (moved || !this.menu.childElementCount) this.#renderMenu();
  }

  #toggle(id) {
    // Auto is exclusive in both directions: picking it drops the rest, and
    // picking a corpus is how you stop delegating the choice.
    let next;
    if (id === AUTO.id) next = this.value.includes(AUTO.id) ? [] : [AUTO.id];
    else if (this.value.includes(AUTO.id)) next = [id];
    else {
      next = this.value.includes(id)
        ? this.value.filter((other) => other !== id)
        : [...this.value, id];
    }
    if (!next.length) return; // searching nothing is not a choice worth offering
    // Keep catalogue order, so the chip reads the same whatever order they were
    // clicked in.
    this.value = this.lenses.map((l) => l.id).filter((candidate) => next.includes(candidate));
    this.#renderTrigger();
    // Update the options in place. The selection round-trips through onChange
    // → settings → set(), which now skips its own repaint, so this is the only
    // visual update, and it must not rebuild the menu either, because the
    // click that caused it is still bubbling (see set()).
    for (const option of this.menu.querySelectorAll('.lens-option')) {
      const on = this.value.includes(option.dataset.lens);
      option.classList.toggle('selected', on);
      option.setAttribute('aria-pressed', String(on));
      option.querySelector('.lens-mark').innerHTML = on ? icon('check', { size: 13 }) : '';
    }
    this.onChange?.([...this.value]);
  }

  #setOpen(open) {
    this.open = open;
    this.menu.hidden = !open;
    this.trigger.setAttribute('aria-expanded', String(open));
  }

  #selected() {
    return this.lenses.filter((lens) => this.value.includes(lens.id));
  }

  #renderTrigger() {
    const selected = this.#selected();
    // Every selected source puts its icon on the chip, so a combined run is
    // visible at a glance; the names live in the tooltip. A single source
    // keeps its spelled-out label.
    this.trigger.innerHTML = selected
      .map((lens) => icon(ICONS[lens.id] ?? 'filter', { size: 15 }))
      .join('');
    if (selected.length <= 1) {
      const label = document.createElement('span');
      label.textContent = selected[0]?.label ?? 'Sources';
      this.trigger.append(label);
    }
    this.trigger.dataset.lenses = this.value.join(' ');
    this.trigger.classList.toggle('active', this.value.some((id) => id !== 'web'));
    this.trigger.title = `Searching: ${selected.map((l) => l.label).join(', ')}`;
  }

  #renderMenu() {
    this.menu.replaceChildren(
      ...this.lenses.map((lens) => {
        const on = this.value.includes(lens.id);
        const option = document.createElement('button');
        option.type = 'button';
        option.className = `lens-option${on ? ' selected' : ''}`;
        option.dataset.lens = lens.id;
        option.setAttribute('aria-pressed', String(on));

        const title = document.createElement('span');
        title.className = 'lens-option-label';
        title.innerHTML = icon(ICONS[lens.id] ?? 'filter', { size: 14 });
        const name = document.createElement('span');
        name.textContent = lens.label;
        const mark = document.createElement('span');
        mark.className = 'lens-mark';
        mark.innerHTML = on ? icon('check', { size: 13 }) : '';
        title.append(name, mark);

        const description = document.createElement('span');
        description.className = 'lens-option-desc';
        description.textContent = lens.description;

        option.append(title, description);
        option.addEventListener('click', () => this.#toggle(lens.id));
        return option;
      }),
      footnote(),
    );
  }
}

function footnote() {
  const note = document.createElement('p');
  note.className = 'lens-foot';
  note.textContent = 'Combine several. Results from all of them are ranked together.';
  return note;
}
