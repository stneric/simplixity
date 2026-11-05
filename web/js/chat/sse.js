// EventSource wrapper + the block store.
//
// EventSource resumes by itself after a *network* drop and replays
// Last-Event-ID, which the server feeds straight to XREAD. That half needs no
// code here. The other half does: per spec, an HTTP error status or a wrong
// content type "fails the connection", meaning readyState goes to CLOSED, one
// error event fires, and the browser never retries. Cloudflare Access answering
// an expired session with a redirect to its login page is exactly that case.
// A proxy that buffers text/event-stream is worse still, neither `open` nor
// `error` ever fires and the connection just hangs.
//
// So this file owns three guarantees the UI leans on:
//
//   * a dead stream is reported as dead, not as "reconnecting" forever
//   * a blocked stream falls back to polling the snapshot endpoint, which is
//     what `/api/runs/{id}` exists for, so the run stays watchable without SSE
//   * `onDone` fires on *every* exit path, mirroring the server's own rule in
//     app/agent/runner.py. The composer stays locked on `state.runId` until it
//     does, so a swallowed exit here is a UI that only F5 can revive.

const TERMINAL = 'done';
const TERMINAL_STATUSES = new Set(['completed', 'error', 'cancelled']);

// No `open` inside this window means something between here and the app is
// sitting on the response. Long enough to survive a slow first byte on a cold
// container, short enough that nobody watches a dead spinner for long.
const OPEN_TIMEOUT_MS = 10_000;
const POLL_INTERVAL_MS = 2_000;
// Consecutive snapshot failures tolerated before the run is given up on. At the
// interval above that is ~20s of silence, which outlasts a redeploy but not a
// user's patience.
const MAX_POLL_FAILURES = 10;

export function applyPatch(doc, ops) {
  for (const op of ops) {
    const tokens = parsePath(op.path);
    if (!tokens.length) continue;
    const key = tokens.pop();
    const parent = resolve(doc, tokens);
    if (parent === undefined) continue;

    switch (op.op) {
      case 'add':
      case 'replace':
        setValue(parent, key, op.value);
        break;
      case 'append': // extension: string delta, see app/store/stream.py
        setValue(parent, key, (getValue(parent, key) ?? '') + String(op.value ?? ''));
        break;
      case 'remove':
        Array.isArray(parent) ? parent.splice(Number(key), 1) : delete parent[key];
        break;
    }
  }
  return doc;
}

const parsePath = (path) =>
  !path || path === '/' ? [] : path.replace(/^\//, '').split('/').map((t) => t.replace(/~1/g, '/').replace(/~0/g, '~'));

const resolve = (doc, tokens) => tokens.reduce((node, t) => (node == null ? undefined : node[t]), doc);
const getValue = (parent, key) => (Array.isArray(parent) && key === '-' ? undefined : parent[key]);

function setValue(parent, key, value) {
  if (Array.isArray(parent)) {
    if (key === '-' || Number(key) === parent.length) parent.push(value);
    else parent[Number(key)] = value;
  } else {
    parent[key] = value;
  }
}

/**
 * Subscribe to a run. Returns { close }; call it to stop early.
 * `onChange(blocks)` fires whenever the store mutates.
 *
 * `onStatus` reports one of: connecting, live, reconnecting, syncing, closed.
 * `syncing` is the polling fallback, the run is still being followed, just
 * without token-level streaming.
 */
export function subscribe(runId, { onChange, onPhase, onTitle, onDone, onStatus }) {
  const blocks = new Map();
  let source = null;
  let closed = false; // terminal: either the run ended or the caller let go
  let opened = false; // SSE reached `open` at least once
  let watchdog = null;
  let pollTimer = null;
  let pollFailures = 0;

  // One render per frame, however many events arrived in it. Rejoining a long
  // run replays its whole log from the first event, thousands of token patches
  // in a burst, and rendering after each one re-parsed the full answer every
  // time and froze the tab until the replay was through.
  let frame = null;
  const schedule = globalThis.requestAnimationFrame ?? ((fn) => setTimeout(fn, 16));
  const unschedule = globalThis.cancelAnimationFrame ?? clearTimeout;
  const flush = () => {
    if (frame === null) return;
    unschedule(frame);
    frame = null;
    onChange?.([...blocks.values()]);
  };
  const emit = () => {
    if (frame === null) frame = schedule(flush);
  };

  function cleanup() {
    clearTimeout(watchdog);
    clearInterval(pollTimer);
    pollTimer = null;
    source?.close();
    source = null;
    document.removeEventListener('visibilitychange', onVisible);
  }

  // The single exit. Everything that ends a run goes through here, so there is
  // no path that leaves the caller believing a run is still in flight.
  function finish(payload) {
    if (closed) return;
    closed = true;
    // The last frame's worth of blocks lands before `onDone` settles the view,
    // or the answer would settle without its final words.
    flush();
    cleanup();
    onStatus?.('closed');
    onDone?.(payload);
  }

  // ── SSE ────────────────────────────────────────────────────────────

  function startSse() {
    source = new EventSource(`/api/runs/${runId}/events`);

    watchdog = setTimeout(() => {
      // Headers never arrived. TLS-terminating AV products and corporate
      // proxies buffer text/event-stream like this, and the browser reports
      // nothing at all, so this timer is the only way to notice.
      if (!opened && !closed) degrade();
    }, OPEN_TIMEOUT_MS);

    source.addEventListener('open', () => {
      opened = true;
      clearTimeout(watchdog);
      onStatus?.('live');
    });

    source.addEventListener('block', (e) => {
      const block = JSON.parse(e.data);
      blocks.set(block.id, block);
      emit();
    });

    source.addEventListener('patch', (e) => {
      const { id, ops } = JSON.parse(e.data);
      const block = blocks.get(id);
      // A patch for an unknown block means we joined mid-run without a replay;
      // the snapshot endpoint covers that case, so dropping it here is correct.
      if (!block) return;
      applyPatch(block, ops);
      emit();
    });

    source.addEventListener('phase', (e) => onPhase?.(JSON.parse(e.data).phase));

    // A new chat's title, written beside the answer (app/runs.py `_title`).
    // Not in the polling fallback: the page reloads the chat list when the run
    // ends, which picks it up there.
    source.addEventListener('title', (e) => onTitle?.(JSON.parse(e.data)));

    source.addEventListener(TERMINAL, (e) => finish(JSON.parse(e.data)));

    source.addEventListener('error', () => {
      if (closed) return;
      // CLOSED is the spec's "fail the connection" path: a non-200 status or the
      // wrong content type. The browser will never retry this one, so calling it
      // "reconnecting" would be a lie the UI never recovers from.
      if (source?.readyState === EventSource.CLOSED) degrade();
      else onStatus?.('reconnecting');
    });
  }

  // ── polling fallback ───────────────────────────────────────────────

  function degrade() {
    if (closed || pollTimer) return;
    source?.close();
    source = null;
    clearTimeout(watchdog);
    onStatus?.('syncing');
    pollTimer = setInterval(poll, POLL_INTERVAL_MS);
    poll(); // do not make the user wait out the first interval
  }

  function countFailure() {
    // A one-off resync check failing says nothing; only the polling path, which
    // is the last transport left, is allowed to give up on the run.
    if (!pollTimer) return;
    if (++pollFailures < MAX_POLL_FAILURES) return;
    finish({
      status: 'error',
      message: 'Lost contact with the server. The run may still be going, reload to check.',
    });
  }

  async function poll() {
    if (closed) return;

    let res;
    try {
      res = await fetch(`/api/runs/${runId}`, { headers: { Accept: 'application/json' } });
    } catch {
      return countFailure();
    }

    // An expired Cloudflare Access session answers with a redirect to its login
    // page, which fetch follows and hands back as HTML. Without this check the
    // JSON parse below would read as a random transport blip and the one thing
    // that would actually help, "sign in again", would never reach the user.
    const isJson = (res.headers.get('content-type') ?? '').includes('application/json');
    if (res.redirected || !isJson) {
      return finish({
        status: 'error',
        message: 'Your session expired. Reload the page to sign in again.',
      });
    }
    if (res.status === 404) {
      return finish({ status: 'error', message: 'This run is no longer available.' });
    }
    if (!res.ok) return countFailure();

    let payload;
    try {
      payload = await res.json();
    } catch {
      return countFailure();
    }
    pollFailures = 0;

    // Snapshots are whole blocks, so they replace rather than patch. Applying
    // one over a partially streamed store is what makes the SSE-to-polling
    // handover invisible.
    for (const block of payload.blocks ?? []) blocks.set(block.id, block);
    emit();

    const state = payload.state ?? {};
    if (state.phase) onPhase?.(state.phase);
    if (TERMINAL_STATUSES.has(state.status)) {
      finish({ status: state.status, message: state.message || undefined });
    }
  }

  // A laptop shut overnight wakes with a stream the browser quietly abandoned
  // and a run that finished hours ago. Re-checking on return is what turns that
  // from a frozen composer into a rendered answer.
  function onVisible() {
    if (closed || document.visibilityState !== 'visible') return;
    if (source && source.readyState === EventSource.CLOSED) degrade();
    else poll();
  }

  onStatus?.('connecting');
  document.addEventListener('visibilitychange', onVisible);
  startSse();

  return {
    close() {
      if (closed) return;
      closed = true;
      // Let go means let go: a frame still pending would render into a view
      // the caller has already moved away from.
      if (frame !== null) unschedule(frame);
      frame = null;
      cleanup();
      onStatus?.('closed');
    },
  };
}
