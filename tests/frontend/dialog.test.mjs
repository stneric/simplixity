// The dialogs that replaced `confirm()` and `prompt()`.
//
// What these pin is the contract the rest of the app leans on: a resolved
// prompt is always a non-empty, trimmed string, and everything else, Escape,
// the backdrop, Cancel, an empty field, resolves null and writes nothing. The
// callers in `web/js/projects/projects.js` do no validating of their own.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { installDom, find, withClass } from './dom.mjs';

installDom();
const { confirmDialog, promptDialog } = await import('../../web/js/shared/dialog.js');

/** The dialog currently on the page. */
const open = () => document.body.children.at(-1);

/** What the browser does with `<form method="dialog">`: run the page's own
 *  submit handler first, and close with the submitter's value unless that
 *  handler cancelled the submission. */
function submit(dialog) {
  const form = find(dialog, 'FORM')[0];
  let prevented = false;
  form.dispatch('submit', { preventDefault: () => (prevented = true) });
  if (!prevented) dialog.close('confirm');
  return prevented;
}

const field = (dialog) => find(dialog, 'INPUT')[0];
const button = (dialog, label) =>
  withClass(dialog, 'confirm-btn').find((node) => node.textContent.startsWith(label));

test('a name comes back trimmed', async () => {
  const answer = promptDialog({ title: 'New project' });
  const dialog = open();
  field(dialog).value = '  Thesis  ';
  submit(dialog);
  assert.equal(await answer, 'Thesis');
});

test('a field holding only spaces does not close the dialog', async () => {
  const answer = promptDialog({ title: 'New project' });
  const dialog = open();
  const input = field(dialog);
  input.value = '   ';
  assert.ok(submit(dialog), 'the submission should have been cancelled');
  assert.equal(dialog.open, true);
  // Cleared, so the browser's own "fill this in" has something to complain
  // about; three spaces satisfy `required` on their own.
  assert.equal(input.value, '');

  // And the dialog is still usable afterwards.
  input.value = 'Later';
  submit(dialog);
  assert.equal(await answer, 'Later');
});

test('backing out resolves null, whichever way it is done', async () => {
  for (const back of [
    (dialog) => button(dialog, 'Cancel').dispatch('click'),
    (dialog) => dialog.close('cancel'), // Escape, and the backdrop
  ]) {
    const answer = promptDialog({ title: 'New project' });
    const dialog = open();
    field(dialog).value = 'typed but abandoned';
    back(dialog);
    assert.equal(await answer, null);
    assert.equal(dialog.removed, true);
  }
});

test('renaming starts from the current name, selected', async () => {
  const answer = promptDialog({ title: 'Rename project', value: 'Old name' });
  const dialog = open();
  const input = field(dialog);
  assert.equal(input.value, 'Old name');
  // Typing over a selection is one keystroke; a focused field would need the
  // name wiped by hand first.
  assert.equal(input.focused, true);
  assert.equal(input.selected, true);
  dialog.close('cancel');
  await answer;
});

test('a name that looks like markup stays text', async () => {
  const answer = promptDialog({ title: 'Rename “<img src=x onerror=1>”' });
  const dialog = open();
  const heading = withClass(dialog, 'confirm-title')[0];
  assert.equal(heading.textContent, 'Rename “<img src=x onerror=1>”');
  assert.equal(heading.children.length, 0, 'the title must never become nodes');
  dialog.close('cancel');
  await answer;
});

test('confirming is true, and only the confirm button is', async () => {
  const yes = confirmDialog({ title: 'Delete it?' });
  open().close('confirm');
  assert.equal(await yes, true);

  const no = confirmDialog({ title: 'Delete it?' });
  const dialog = open();
  // Cancel holds focus, so Return on a dialog nobody read cannot delete.
  assert.equal(button(dialog, 'Cancel').focused, true);
  dialog.close('cancel');
  assert.equal(await no, false);
});

test('each dialog says which key does what', async () => {
  const answer = promptDialog({ title: 'New project' });
  const dialog = open();
  const caps = withClass(dialog, 'kbd').map((node) => node.textContent);
  assert.deepEqual(caps, ['Esc', '⏎']);
  dialog.close('cancel');
  await answer;
});
