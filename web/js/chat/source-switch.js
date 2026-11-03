// The composer's one control in a literature project: Local or Web.
//
// A project with a reading list answers from the papers screened there, and
// nothing else (app/search/library.py). The depth and source chips make no
// sense in it: the corpus is fixed and the router picks the depth. What the
// person does need is a way to look beyond their papers on purpose, for one
// question, and to see at a glance which of the two the next answer will be.
// That is this switch, and it is the only thing the composer shows there.
//
// Local is the default every time a project is entered, never remembered
// across reloads: a thesis answer that quietly came from the web because the
// switch was left on yesterday is the failure the whole arrangement exists
// to prevent. The server holds the same line (`ChatRequest.web` is off unless
// sent), so a stale client fails safe.

/** Whether a project from /api/projects is a literature project. */
export function isLibrary(project) {
  return Boolean(project && Number(project.lists) > 0);
}

/** What the next answer is written from, shown when hovering the switch. */
export function libraryBadge(project, web) {
  if (!isLibrary(project)) return '';
  const texts = Number(project.fulltexts) || 0;
  const relevant = Number(project.relevant) || 0;
  const what = relevant
    ? `${relevant} relevant paper${relevant === 1 ? '' : 's'}, ${texts} with full text`
    : 'no papers marked relevant yet';
  return web
    ? `“${project.name}”: your papers (${what}) and the web`
    : `“${project.name}”: answers only from your papers (${what})`;
}

export class SourceSwitch {
  /**
   * @param {HTMLElement} root
   * @param {(web: boolean) => void} onChange
   */
  constructor(root, onChange) {
    this.root = root;
    this.onChange = onChange;
    this.web = false;
    // What "local" is: the project's papers, or the files attached to the chat.
    this.noun = 'your papers';
    this.root.classList.add('source-switch');

    this.button = document.createElement('button');
    this.button.type = 'button';
    this.button.className = 'source-toggle';
    this.button.setAttribute('role', 'switch');
    const local = document.createElement('span');
    local.className = 'source-side local';
    local.textContent = 'Local';
    const web = document.createElement('span');
    web.className = 'source-side web';
    web.textContent = 'Web';
    const knob = document.createElement('span');
    knob.className = 'source-knob';
    this.button.append(local, knob, web);
    // The explanation lives on the switch, shown on hover and on keyboard
    // focus, rather than as a permanent line above the composer: it matters
    // when deciding, and is noise the rest of the time.
    this.tip = document.createElement('span');
    this.tip.className = 'source-tip';
    this.tip.setAttribute('role', 'tooltip');
    this.tip.id = 'source-tip';
    this.button.setAttribute('aria-describedby', this.tip.id);
    this.button.addEventListener('click', () => {
      this.set(!this.web);
      this.onChange?.(this.web);
    });
    this.root.append(this.button, this.tip);
    this.set(false);
  }

  set(web) {
    this.web = Boolean(web);
    this.button.setAttribute('aria-checked', String(this.web));
    this.button.dataset.web = String(this.web);
    this.button.setAttribute('aria-label', this.web ? `Searching ${this.noun} and the web` : `Searching only ${this.noun}`);
  }

  /** The hover text: what the next answer is written from, and what a click does. */
  setTip(text, noun = 'your papers') {
    this.noun = noun;
    this.set(this.web);
    const action = this.web ? `Click to answer from ${noun} only.` : 'Click to search the web as well.';
    this.tip.textContent = text ? `${text}. ${action}` : action;
  }
}
