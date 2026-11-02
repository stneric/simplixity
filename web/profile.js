// Profiles: whose chats these are, chosen when the page opens.
//
// The server decides (app/api/deps.py `current_user`): every personal endpoint
// answers 401 until this browser holds a session, and `GET /api/profiles` says
// whether it does. So the page asks first, and while the answer is "nobody" it
// shows the picker in front of everything and holds the rest of boot until a
// profile is chosen. Nothing behind it has loaded yet, so there is nothing of
// the previous person's to leak onto the screen for a frame.
//
// A password is optional per profile. Keeping someone out of a housemate's
// history on a local install is the whole ambition; see app/store/profiles.py.
//
// Switching reloads the page. Every module that holds a chat list, a project or
// a settings snapshot would otherwise need to be told, and the one thing a
// reload is guaranteed to get right is starting over.

import { iconNode } from './icons.js';

/** The name as the server will accept it: lowercase, `a-z0-9_-`, 32 at most.
 *  Umlauts and spaces are folded rather than rejected, so "Jürgen M" becomes
 *  "jurgen-m" instead of an error the person cannot act on. */
export function slug(name) {
  return String(name ?? '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32);
}

function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function field(labelText, type, { placeholder = '', autocomplete = 'off' } = {}) {
  const id = `profile-${Math.random().toString(36).slice(2, 9)}`;
  const label = el('label', 'prompt-label', labelText);
  label.htmlFor = id;
  const input = el('input', 'prompt-input');
  input.id = id;
  input.type = type;
  input.placeholder = placeholder;
  input.autocomplete = autocomplete;
  input.maxLength = type === 'password' ? 200 : 32;
  return { label, input };
}

/** POST or PUT JSON; resolves `{ ok, data }` with the server's `detail` as the
 *  message on failure, so every form can show why rather than just that. */
async function send(method, url, body) {
  try {
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    });
    const data = await res.json().catch(() => ({}));
    const detail = typeof data.detail === 'string' ? data.detail : 'That did not work.';
    return { ok: res.ok, status: res.status, data, message: res.ok ? '' : detail };
  } catch {
    return { ok: false, status: 0, data: {}, message: 'The server is not answering.' };
  }
}

// ── the picker ───────────────────────────────────────────────────────

/**
 * Resolve the signed-in profile, showing the picker first if there is none.
 *
 * @returns {Promise<{name: string, protected: boolean}>}
 */
export async function ensureProfile() {
  let state = { current: null, profiles: [] };
  try {
    state = await fetch('/api/profiles').then((r) => r.json());
  } catch {
    // Fall through to the picker, whose forms report the server being down.
  }
  const find = (name) => state.profiles?.find((p) => p.name === name);
  if (state.current) return { name: state.current, protected: Boolean(find(state.current)?.protected) };
  return pick(state.profiles ?? []);
}

function pick(profiles) {
  return new Promise((resolve) => {
    // A modal <dialog> makes everything behind it inert, which a positioned
    // div would not. Escape is swallowed: there is no page to go back to.
    const gate = el('dialog', 'profile-gate');
    gate.addEventListener('cancel', (event) => event.preventDefault());
    const card = el('div', 'profile-card');
    gate.append(card);

    const done = (result) => {
      gate.close();
      gate.remove();
      resolve(result);
    };

    card.append(
      el('h1', 'profile-title', 'Who is asking?'),
      el(
        'p',
        'profile-lede',
        profiles.length
          ? 'Choose your profile. Each one has its own chats, projects and settings.'
          : 'Create a profile to start. Each one has its own chats, projects and settings.'
      )
    );

    // Existing profiles, one button each. A protected one opens the password
    // form below instead of signing in.
    const list = el('div', 'profile-options');
    const login = el('form', 'profile-login');
    login.hidden = true;
    const pw = field('Password', 'password', { autocomplete: 'current-password' });
    pw.input.required = true;
    const loginError = el('p', 'profile-error');
    const loginActions = el('div', 'confirm-actions');
    const back = el('button', 'confirm-btn', 'Back');
    back.type = 'button';
    const signIn = el('button', 'confirm-btn primary', 'Sign in');
    signIn.type = 'submit';
    loginActions.append(back, signIn);
    login.append(pw.label, pw.input, loginError, loginActions);

    let chosen = null;
    const showList = () => {
      login.hidden = true;
      list.hidden = false;
      create.hidden = false;
      chosen = null;
    };
    back.addEventListener('click', showList);

    const enter = async (profile, password = '') => {
      const result = await send('POST', '/api/profiles/login', { name: profile.name, password });
      if (result.ok) done({ name: profile.name, protected: profile.protected });
      return result;
    };

    for (const profile of profiles) {
      const option = el('button', 'profile-option');
      option.type = 'button';
      option.append(iconNode('user', 16), el('span', 'profile-option-name', profile.name));
      if (profile.protected) {
        const lock = iconNode('lock', 14);
        lock.title = 'Password protected';
        option.append(lock);
      }
      option.addEventListener('click', async () => {
        if (!profile.protected) {
          const result = await enter(profile);
          if (!result.ok) createError.textContent = result.message;
          return;
        }
        chosen = profile;
        pw.label.textContent = `Password for ${profile.name}`;
        pw.input.value = '';
        loginError.textContent = '';
        list.hidden = true;
        create.hidden = true;
        login.hidden = false;
        pw.input.focus();
      });
      list.append(option);
    }

    login.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!chosen) return;
      const result = await enter(chosen, pw.input.value);
      if (!result.ok) {
        loginError.textContent = result.status === 401 ? 'Wrong password.' : result.message;
        pw.input.select();
      }
    });

    // A new profile: a name, and a password only if wanted.
    const create = el('form', 'profile-new');
    const name = field(profiles.length ? 'New profile' : 'Profile name', 'text', {
      placeholder: 'e.g. anna',
    });
    name.input.required = true;
    const secret = field('Password (optional)', 'password', {
      placeholder: 'Leave empty for none',
      autocomplete: 'new-password',
    });
    const createError = el('p', 'profile-error');
    const createActions = el('div', 'confirm-actions');
    const createBtn = el('button', 'confirm-btn primary', 'Create');
    createBtn.type = 'submit';
    createActions.append(createBtn);
    create.append(name.label, name.input, secret.label, secret.input, createError, createActions);

    create.addEventListener('submit', async (event) => {
      event.preventDefault();
      const wanted = slug(name.input.value);
      if (!wanted) {
        createError.textContent = 'Use letters or numbers.';
        return;
      }
      const result = await send('POST', '/api/profiles', {
        name: wanted,
        password: secret.input.value,
      });
      if (result.ok) done({ name: wanted, protected: Boolean(secret.input.value) });
      else createError.textContent = result.message;
    });

    if (profiles.length) card.append(list, login);
    card.append(create);
    document.body.append(gate);
    gate.showModal();
    (profiles.length ? list.firstChild : name.input)?.focus();
  });
}

// ── the sidebar entry ────────────────────────────────────────────────

/**
 * Print the current profile on `button`; a click offers switching and the
 * password.
 *
 * @param {HTMLElement} button   the sidebar entry
 * @param {HTMLElement} nameSlot where the name is printed
 * @param {{name: string, protected: boolean}} profile
 */
export function mountProfile(button, nameSlot, profile) {
  nameSlot.textContent = profile.name;
  button.title = 'Profile';
  button.addEventListener('click', () => openMenu(profile));
}

function openMenu(profile) {
  const dialog = el('dialog', 'confirm profile-menu');
  dialog.append(el('h2', 'confirm-title', profile.name));
  dialog.append(
    el(
      'p',
      'confirm-body',
      profile.protected ? 'This profile has a password.' : 'This profile has no password.'
    )
  );
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => dialog.remove());

  const form = el('form', 'profile-password');
  const current = profile.protected
    ? field('Current password', 'password', { autocomplete: 'current-password' })
    : null;
  if (current) {
    current.input.required = true;
    form.append(current.label, current.input);
  }
  const next = field(profile.protected ? 'New password' : 'Set a password', 'password', {
    placeholder: profile.protected ? 'Leave empty to remove it' : '',
    autocomplete: 'new-password',
  });
  next.input.required = !profile.protected;
  const error = el('p', 'profile-error');
  const actions = el('div', 'confirm-actions');
  const switchBtn = el('button', 'confirm-btn', 'Switch profile');
  switchBtn.type = 'button';
  const save = el('button', 'confirm-btn primary', profile.protected ? 'Save' : 'Set password');
  save.type = 'submit';
  actions.append(switchBtn, save);
  form.append(next.label, next.input, error, actions);
  dialog.append(form);

  switchBtn.addEventListener('click', async () => {
    await send('POST', '/api/profiles/logout');
    // The open chat belongs to the profile being left.
    location.replace(location.pathname);
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const result = await send('PUT', '/api/profiles/password', {
      current: current?.input.value ?? '',
      password: next.input.value,
    });
    if (!result.ok) {
      error.textContent = result.status === 401 ? 'Wrong password.' : result.message;
      return;
    }
    profile.protected = Boolean(next.input.value);
    dialog.close();
  });

  document.body.append(dialog);
  dialog.showModal();
  (current?.input ?? next.input).focus();
}
