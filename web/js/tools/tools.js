// Tools: single-purpose jobs beside the research chat, behind one button at the
// foot of the sidebar.
//
// A registry rather than a button per tool, so the sidebar does not grow a row
// for every tool added and each tool stays a module of its own, imported only
// when it is opened. `local` is shown as a badge: a tool that refuses remote
// models should say so before anyone pastes a contract into it.

import { iconNode } from '../shared/icons.js';
import { el } from '../shared/dom.js';

export const TOOLS = [
  {
    id: 'literature',
    label: 'Literature',
    icon: 'library',
    local: false,
    description:
      'Find scientific papers for a question, screen their abstracts against your criteria, and export the relevant ones as BibTeX.',
    // A page of its own, not a dialog: a reading list is days of work and
    // wants an address. See web/js/literature/literature.js.
    open: () => location.assign('/literature'),
  },
  {
    id: 'redact',
    label: 'Redact',
    icon: 'redact',
    local: true,
    description:
      'Black out names, addresses, account numbers and secrets in a document before you paste it anywhere else.',
    open: (ctx) => import('./redact-view.js').then((m) => m.openRedact(ctx)),
  },
];

/** The list of tools, as a small dialog. Choosing one closes it and opens the
 *  tool, which brings its own dialog. */
export function openToolMenu(ctx) {
  const dialog = el('dialog', 'confirm tool-menu');
  dialog.append(el('h2', 'confirm-title', 'Tools'));
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => dialog.remove());

  const list = el('div', 'tool-options');
  for (const tool of TOOLS) {
    const option = el('button', 'tool-option');
    option.type = 'button';
    const text = el('span', 'tool-option-text');
    const head = el('span', 'tool-option-head');
    head.append(el('span', 'tool-option-name', tool.label));
    if (tool.local) head.append(el('span', 'tool-badge', 'Local model'));
    text.append(head, el('span', 'tool-option-desc', tool.description));
    option.append(iconNode(tool.icon, 18), text);
    option.addEventListener('click', () => {
      dialog.close();
      tool.open(ctx);
    });
    list.append(option);
  }
  dialog.append(list);

  document.body.append(dialog);
  dialog.showModal();
  list.firstChild?.focus();
}

/** The sidebar entry. */
export function mountTools(button, ctx) {
  button.addEventListener('click', () => openToolMenu(ctx));
}
