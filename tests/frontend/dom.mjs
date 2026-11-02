// The smallest DOM that `web/markdown.js`, `web/blocks.js` and the two drawing
// modules need.
//
// A real headless browser would test more, but it would also be a toolchain.
// This grows only when a renderer reaches for something it does not have
// (createElementNS for the SVG modules, classList and style for the widgets),
// and it stays a plain tree that a test can assert on directly.

class ClassList {
  constructor(owner) {
    this.owner = owner;
  }
  #names() {
    return this.owner.className.split(/\s+/).filter(Boolean);
  }
  add(...names) {
    this.owner.className = [...new Set([...this.#names(), ...names])].join(' ');
  }
  remove(...names) {
    this.owner.className = this.#names()
      .filter((n) => !names.includes(n))
      .join(' ');
  }
  contains(name) {
    return this.#names().includes(name);
  }
  toggle(name, force) {
    const on = force ?? !this.contains(name);
    if (on) this.add(name);
    else this.remove(name);
    return on;
  }
}

class Element {
  constructor(tag, namespace = '') {
    this.tagName = (tag || '').toUpperCase();
    this.namespaceURI = namespace;
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.classList = new ClassList(this);
    this.listeners = {};
    this._text = '';
  }

  append(...nodes) {
    for (const node of nodes) if (node) node.parentNode = this;
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    for (const node of nodes) if (node) node.parentNode = this;
    this.children = [...nodes];
  }

  /** `removed` is the flag a test asserts on; detaching is what the browser
   *  would do, and a fallback that only sets a flag would pass either way. */
  remove() {
    this.removed = true;
    const siblings = this.parentNode?.children;
    if (siblings) this.parentNode.children = siblings.filter((node) => node !== this);
  }

  /** Sibling insertion, for the tray `BlockView` mounts after its container. */
  after(node) {
    const siblings = this.parentNode?.children;
    if (!siblings) return;
    siblings.splice(siblings.indexOf(this) + 1, 0, node);
    node.parentNode = this.parentNode;
  }

  before(node) {
    const siblings = this.parentNode?.children;
    if (!siblings) return;
    siblings.splice(siblings.indexOf(this), 0, node);
    node.parentNode = this.parentNode;
  }

  replaceWith(node) {
    const siblings = this.parentNode?.children;
    if (!siblings) return;
    this.parentNode.children = siblings.map((child) => (child === this ? node : child));
    node.parentNode = this.parentNode;
    this.removed = true;
  }

  addEventListener(type, handler) {
    (this.listeners[type] ??= []).push(handler);
  }

  /** Fire a listener the way the browser would, to assert on what a
   *  fallback does, which is the half of image handling that never fires in
   *  production because the proxies answer failures with a 1×1 pixel. */
  dispatch(type, event = {}) {
    for (const handler of this.listeners[type] ?? []) handler({ target: this, ...event });
  }

  /** Just enough <dialog> for `web/dialog.js`: it opens one, reads
   *  `returnValue` on close, and resolves from the close event. The focus
   *  calls are no-ops here but are what a browser would need to have. */
  showModal() {
    this.open = true;
  }

  close(value = '') {
    this.open = false;
    this.returnValue = value;
    this.dispatch('close');
  }

  focus() {
    this.focused = true;
  }

  select() {
    this.selected = true;
  }

  removeAttribute(name) {
    delete this.attrs[name];
  }

  reportValidity() {
    this.reported = true;
    return Boolean(this.value);
  }

  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }

  getAttribute(name) {
    return this.attrs[name] ?? null;
  }

  /** Enough of the tree API for `BlockView` to reconcile against, which is the
   *  half of `blocks.js` that a renderer test cannot reach, and where the
   *  interesting bugs live: the view decides which node survives a patch and
   *  what state carries across it. */
  get childNodes() {
    return this.children;
  }

  get lastChild() {
    return this.children[this.children.length - 1] ?? null;
  }

  appendChild(node) {
    this.append(node);
    return node;
  }

  insertBefore(node, reference) {
    const at = this.children.indexOf(reference);
    node.parentNode = this;
    if (at === -1) this.children.push(node);
    else this.children.splice(at, 0, node);
    return node;
  }

  /** `tag`, `.class`, `.a.b`, and one level of `A > B` (`:scope` included).
   *  Everything the view and the renderers actually ask for, and nothing else,
   *  a selector this cannot parse should fail loudly rather than match wrongly. */
  querySelector(selector) {
    return queryAll(this, selector)[0] ?? null;
  }

  querySelectorAll(selector) {
    return queryAll(this, selector);
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text;
  }

  set href(value) {
    this.attrs.href = value;
  }
  get href() {
    return this.attrs.href;
  }

  set src(value) {
    this.attrs.src = value;
  }
  get src() {
    return this.attrs.src;
  }

  set title(value) {
    this.attrs.title = value;
  }
  get title() {
    return this.attrs.title;
  }

  /** Compact shape for assertions: {TAG: [children]} or {TAG: "text"}. */
  get outline() {
    const kids = this.children.map((c) => c.outline).filter((c) => c !== null);
    return { [this.tagName]: kids.length ? kids : this.textContent };
  }
}

function matchOne(node, selector) {
  if (!node.tagName) return false;
  if (selector.startsWith('.')) {
    return selector
      .slice(1)
      .split('.')
      .every((name) => node.classList.contains(name));
  }
  if (/^[a-zA-Z][\w-]*$/.test(selector)) return node.tagName === selector.toUpperCase();
  throw new Error(`the test DOM cannot parse the selector "${selector}"`);
}

function descendants(node, found = []) {
  for (const child of node.children ?? []) {
    if (!child.tagName) continue;
    found.push(child);
    descendants(child, found);
  }
  return found;
}

function queryAll(node, selector) {
  const parts = selector.split('>').map((part) => part.trim());
  if (parts.length === 1) return descendants(node).filter((n) => matchOne(n, selector));
  if (parts.length !== 2) throw new Error(`the test DOM cannot parse "${selector}"`);
  const [left, right] = parts;
  const parents = left === ':scope' ? [node] : queryAll(node, left);
  return parents.flatMap((parent) =>
    (parent.children ?? []).filter((child) => matchOne(child, right)),
  );
}

class Text {
  constructor(value) {
    this._text = String(value);
  }
  get textContent() {
    return this._text;
  }
  get outline() {
    return this._text === '' ? null : this._text;
  }
}

export function installDom() {
  globalThis.document = {
    createElement: (tag) => new Element(tag),
    createElementNS: (ns, tag) => new Element(tag, ns),
    createTextNode: (value) => new Text(value),
    addEventListener() {},
    body: new Element('body'),
  };
  globalThis.window = { addEventListener() {} };
  globalThis.location = { origin: 'http://localhost:3000' };
}

/** Every node carrying a class, in document order. */
export function withClass(node, className, found = []) {
  if (node.classList?.contains(className)) found.push(node);
  for (const child of node.children ?? []) withClass(child, className, found);
  return found;
}

/** The visible text of the first node with this class, or "". */
export function textOf(node, className) {
  return withClass(node, className)[0]?.textContent ?? '';
}

/** Every href/src the tree produced: what a URL sanitiser has to get right. */
export function urls(node, found = []) {
  if (node.attrs?.href) found.push(node.attrs.href);
  if (node.attrs?.src) found.push(node.attrs.src);
  for (const child of node.children ?? []) urls(child, found);
  return found;
}

export function tags(node, found = []) {
  if (node.tagName) found.push(node.tagName);
  for (const child of node.children ?? []) tags(child, found);
  return found;
}

/** The elements of a given tag, in document order, to assert on what a
 *  node carries (an alignment, a class) rather than merely that it exists. */
export function find(node, tagName, found = []) {
  if (node.tagName === tagName.toUpperCase()) found.push(node);
  for (const child of node.children ?? []) find(child, tagName, found);
  return found;
}
