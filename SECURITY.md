# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's
[private vulnerability reporting](https://github.com/stneric/simplixity/security/advisories/new)
instead.

Helpful details: the version or commit, how you run the app (Docker or not,
behind which proxy), steps to reproduce, and what an attacker gains.

## What the app does and does not protect

Simplixity is meant for a single person or a small group on a machine they
control. Knowing its limits helps you deploy it safely and tells you what
counts as a vulnerability.

In scope:

- Model output reaching the page as markup or script. The frontend never
  assigns `innerHTML` from model output and the Content Security Policy allows
  only the app's own origin; a way around either is a vulnerability.
- One profile reading or changing another profile's chats, projects, files or
  settings.
- The image and favicon proxies being used to reach internal addresses
  (server-side request forgery).
- API keys leaking into responses, logs or error messages.

Out of scope, by design:

- **Profiles are not hardened authentication.** There is no login rate limit
  and no password recovery. Do not expose the app to the internet without an
  access layer in front of it (an authenticating reverse proxy, a VPN, or
  similar).
- **No TLS of its own.** The compose file binds to `127.0.0.1`; terminate TLS in
  a reverse proxy.
- Rate limiting (`RATE_LIMIT_RUNS_PER_MINUTE`) protects your API budget from a
  runaway client. It is not a defence against a determined attacker.
