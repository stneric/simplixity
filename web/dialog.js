// Confirmation and single-field dialogs on the native <dialog> element.
//
// `confirm()` and `prompt()` were doing three things badly at once: they render
// in OS chrome that shares nothing with the app's type or colour, they block
// the event loop while they are up, and on mobile they read as a browser
// warning about the site rather than as a decision inside the product.
// `showModal()` keeps the parts that made them worth reaching for, focus
// trapping, Escape to dismiss, everything behind them inert, and drops the
// rest. `prompt()` additionally has no field label, no length limit and no
// placeholder, and Safari on iOS refuses to show it at all in a few contexts.
//
// Text goes in through `textContent` only. Chat titles and project names reach
// this file straight from user input and model output, so the one thing this
// module must never grow is an innerHTML.

import { keycap } from './shortcuts.js';

/** The surface both dialogs share: a card, a question, and an optional line of
 *  consequence under it. Closing on a backdrop click is here too, it lands on
 *  the dialog element itself and never on a child, which is what makes the
 *  test reliable. */
function shell(title, body) {
  const dialog = document.createElement('dialog');
  dialog.className = 'confirm';

  const heading = document.createElement('h2');
  heading.className = 'confirm-title';
  heading.textContent = title;
  dialog.append(heading);

  if (body) {
    const text = document.createElement('p');
    text.className = 'confirm-body';
    text.textContent = body;
    dialog.append(text);
  }

  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close('cancel');
  });
  return dialog;
}

/** A button with its keycap tucked in beside the label, so a dialog says which
 *  key does what rather than leaving it to be guessed. */
function button(label, { className = 'confirm-btn', keys = null, type = 'submit', value } = {}) {
  const node = document.createElement('button');
  node.type = type;
  if (value) node.value = value;
  node.className = className;
  node.append(document.createTextNode(label));
  if (keys) node.append(keycap(keys));
  return node;
}

/**
 * Ask for confirmation. Resolves true only if the confirm button was used;
 * Escape, the backdrop and the cancel button all resolve false.
 *
 * @param {object}  opts
 * @param {string}  opts.title    the decision, as a question
 * @param {string} [opts.body]    the consequence, when it is not obvious
 * @param {string} [opts.confirm] confirm button label
 * @param {string} [opts.cancel]  cancel button label
 * @param {boolean}[opts.danger]  red confirm button, for destructive actions
 * @returns {Promise<boolean>}
 */
export function confirmDialog({
  title,
  body = '',
  confirm = 'Delete',
  cancel = 'Cancel',
  danger = true,
} = {}) {
  return new Promise((resolve) => {
    const dialog = shell(title, body);

    // method="dialog" closes on submit and carries the button's value into
    // returnValue, so neither button needs a click handler of its own.
    const actions = document.createElement('form');
    actions.method = 'dialog';
    actions.className = 'confirm-actions';

    // Cancel is the one that gets the keycap, because cancel is what has
    // focus: on this dialog both Escape and Return back out of it.
    const cancelBtn = button(cancel, { value: 'cancel', keys: 'escape' });
    const confirmBtn = button(confirm, {
      value: 'confirm',
      className: `confirm-btn primary${danger ? ' danger' : ''}`,
    });

    actions.append(cancelBtn, confirmBtn);
    dialog.append(actions);

    dialog.addEventListener('close', () => {
      const confirmed = dialog.returnValue === 'confirm';
      dialog.remove();
      resolve(confirmed);
    });

    document.body.append(dialog);
    dialog.showModal();
    // Cancel takes focus, so Enter on a dialog nobody read cannot delete
    // anything. Confirming is always a deliberate second step.
    cancelBtn.focus();
  });
}

/**
 * Ask for one line of text. Resolves the trimmed value, or null if the dialog
 * was backed out of. Submitting is only possible with something in the field,
 * so a resolved value is never empty.
 *
 * @param {object}  opts
 * @param {string}  opts.title       what is being named
 * @param {string} [opts.body]       what the name is for, when it is not obvious
 * @param {string} [opts.label]      field label; sits above the input
 * @param {string} [opts.value]      starting value, selected on open
 * @param {string} [opts.placeholder]
 * @param {number} [opts.maxlength]
 * @param {string} [opts.confirm]    submit button label
 * @param {string} [opts.cancel]     cancel button label
 * @returns {Promise<string|null>}
 */
export function promptDialog({
  title,
  body = '',
  label = 'Name',
  value = '',
  placeholder = '',
  maxlength = 120,
  confirm = 'Create',
  cancel = 'Cancel',
} = {}) {
  return new Promise((resolve) => {
    const dialog = shell(title, body);

    const form = document.createElement('form');
    form.method = 'dialog';
    form.className = 'prompt-form';

    const id = `prompt-${Math.random().toString(36).slice(2, 9)}`;
    const caption = document.createElement('label');
    caption.className = 'prompt-label';
    caption.htmlFor = id;
    caption.textContent = label;

    const input = document.createElement('input');
    input.type = 'text';
    input.id = id;
    input.className = 'prompt-input';
    input.value = value;
    input.placeholder = placeholder;
    input.maxLength = maxlength;
    input.autocomplete = 'off';
    // Enter submits the form, which is the whole reason this is a <form>, and
    // `required` is what stops it from submitting an empty one.
    input.required = true;

    const actions = document.createElement('div');
    actions.className = 'confirm-actions';

    // Not a submit button: the form's only submitter must be the confirm one,
    // or Enter in the field would cancel.
    const cancelBtn = button(cancel, { type: 'button', keys: 'escape' });
    cancelBtn.addEventListener('click', () => dialog.close('cancel'));
    const confirmBtn = button(confirm, {
      value: 'confirm',
      className: 'confirm-btn primary',
      keys: 'enter',
    });

    // A name of three spaces passes `required` and would be stored as one.
    form.addEventListener('submit', (event) => {
      if (input.value.trim()) return;
      event.preventDefault();
      input.value = '';
      input.reportValidity();
    });

    actions.append(cancelBtn, confirmBtn);
    form.append(caption, input, actions);
    dialog.append(form);

    dialog.addEventListener('close', () => {
      const text = input.value.trim();
      dialog.remove();
      resolve(dialog.returnValue === 'confirm' && text ? text : null);
    });

    document.body.append(dialog);
    dialog.showModal();
    // Selected rather than merely focused: renaming usually replaces the whole
    // name, and typing over a selection is one keystroke instead of a wipe.
    input.focus();
    input.select();
  });
}
