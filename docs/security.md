# Security

What stemtrace does with sensitive task data, and how to keep the dashboard private.

- [Sensitive data scrubbing](#sensitive-data-scrubbing)
- [Protecting the standalone server](#protecting-the-standalone-server)
- [API key for scripts and coding agents](#api-key-for-scripts-and-coding-agents)
- [Protecting an embedded dashboard](#protecting-an-embedded-dashboard)
  - [Custom authentication](#custom-authentication)
  - [Built-in login page](#built-in-login-page)
  - [Built-in auth helpers (Basic / API key)](#built-in-auth-helpers-basic--api-key)

## Sensitive data scrubbing

By default, stemtrace scrubs common sensitive keys from task arguments:
- Passwords: `password`, `passwd`, `pwd`, `secret`
- API keys: `api_key`, `apikey`, `token`, `bearer`, `authorization`
- Financial: `credit_card`, `cvv`, `ssn`
- Session: `cookie`, `session`, `csrf`

Scrubbed values appear as `[Filtered]` in the UI.

Scrubbing goes by key name: keyword arguments, and keys of dicts inside arguments and results.
Values that aren't dicts, lists or tuples, whether positional arguments or return values (e.g.
the password in `login.delay("alice", "hunter2")`), exception messages and tracebacks are shown
as they are.

Add or exempt keys with `additional_sensitive_keys` and `safe_keys`, or turn scrubbing off with
`scrub_sensitive_data=False` (see [Worker options](configuration.md#worker-options)). Task output
(`capture_output`) is free text and is not scrubbed; see
[Task output](configuration.md#task-output-print-and-logging).

## Protecting the standalone server

```bash
stemtrace server \
    --broker-url redis://myredis:6379/0 \
    --login-username admin \
    --login-password secret \
    --login-secret change-me
```

Set `--login-secret` (or `STEMTRACE_LOGIN_SECRET`) to a long random value, and use the same
value on every replica. Without it, each process generates its own random secret, so sessions
only work within a single process and are lost on restart. Failed sign-ins are throttled per IP
and per username or browser; see [Built-in login page](#built-in-login-page)
for the limits and the proxy setting (`FORWARDED_ALLOW_IPS`, set to your proxies' addresses,
never `*`) that keeps client IPs correct.

## API key for scripts and coding agents

Give scripts and coding agents read access to the JSON API and the WebSocket with one static
key, sent as `Authorization: Bearer <key>`:

```bash
export STEMTRACE_API_KEY="$(openssl rand -hex 32)"
stemtrace server --broker-url redis://myredis:6379/0   # or --api-key, or init_app(api_key=...)

curl -H "Authorization: Bearer $STEMTRACE_API_KEY" \
  "http://localhost:8000/stemtrace/api/tasks?state=FAILURE&limit=20"
```

- **Which requests need it.** Every JSON API route except the liveness probe
  (`/api/health/live`), plus the WebSocket. The UI pages and assets stay as they are. With only
  a key, the UI shell loads but its data calls get `401`, so enable the login page for browsers.
- **With the [login page](#built-in-login-page)** (`--login-username`/`--login-password`), a request passes with a
  session cookie *or* the key. A Bearer header on a UI page is ignored (you still get the login
  page). With a custom `auth_dependency`, a request needs the key *and* whatever the dependency
  checks, so the dependency mustn't use the `Authorization` header itself (`require_basic_auth`
  does; `require_api_key`, which reads `X-API-Key`, doesn't). `require_api_key` is a separate,
  older helper for embedded apps (see [Built-in auth helpers](#built-in-auth-helpers-basic--api-key));
  the key described here is the one built into the server.
- **Errors.** A missing or wrong key gets `401` with `WWW-Authenticate: Bearer`, never a
  redirect. A wrong key is refused even if a valid session cookie comes with it.
- **Throttling.** Wrong keys count per client IP in the login's failed-attempt throttle, so
  failed sign-ins and wrong keys from one IP add up: after 5, that IP gets `429` with
  `Retry-After` (30 seconds, doubling up to 15 minutes), even with the right key. A valid key
  doesn't clear the count. Requests without a client address (a Unix socket) aren't throttled.
  Counts are per process.
- **WebSocket.** Clients that can set headers send `Authorization: Bearer <key>`. Browsers can't,
  so they offer two subprotocols, `stemtrace` and `stemtrace.bearer.<base64url(key)>`
  (unpadded); the server accepts with `stemtrace` and never echoes the key. The key is never
  accepted as a query parameter, because uvicorn's access log prints query strings.
- **The key** must be at least 16 visible ASCII characters (`openssl rand -hex 32` makes a good
  one). Surrounding whitespace is dropped, and a blank value means no key. stemtrace never logs
  it; `stemtrace server` prints `API key: set`. Prefer the env var to `--api-key`, since
  command-line flags show up in process lists. Changing the key needs a restart.
- **For agents:** [`skills/stemtrace-api/SKILL.md`](../skills/stemtrace-api/SKILL.md) teaches a
  coding agent to answer "what failed in the last hour and why" with `curl` and `jq`.

## Protecting an embedded dashboard

### Custom authentication

```python
from fastapi import Depends
import stemtrace
from your_app.auth import require_admin

stemtrace.init_app(app, broker_url="redis://localhost:6379/0", auth_dependency=Depends(require_admin))
```

### Built-in login page

(Recommended for UI-first use.) If you primarily use the UI, the built-in form login (cookie
session) protects **UI + assets + API + WebSocket** with three arguments:

```python
import stemtrace

stemtrace.init_app(
    app,
    broker_url="redis://localhost:6379/0",
    login_username="admin",
    login_password="secret",
    login_secret="change-me",  # recommended for production
)
```

This serves a sign-in page at `/stemtrace/login` and protects the UI, API and WebSocket under
`/stemtrace`. The static JS/CSS bundle under `/stemtrace/assets/` is the public UI build and is
served without a session. So is `/stemtrace/api/health/live`, a liveness probe for load
balancers and container health checks: it always answers 200 while the process is serving,
with only `{"status": "ok"}`, or `{"status": "degraded"}` when the embedded consumer stopped
unexpectedly or can't read from the broker (the same rule as `/stemtrace/api/health`).

For any multi-process deployment (`uvicorn --workers N`, gunicorn, or several replicas behind
a load balancer), set `login_secret` (or the `STEMTRACE_LOGIN_SECRET` env var) to the same long
random value everywhere, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Without it, each process signs sessions with its own random secret, and users get logged out
at random. stemtrace logs a warning at startup in that case, and the login page and UI explain
the problem when a session signed by another process is rejected.

Failed sign-ins are throttled in memory, per client IP and per username or browser:

- After 5 failures from one IP (IPv6: one /64 network), further attempts from it get
  `429 Too Many Requests` with a `Retry-After` header, without the credentials being checked.
  The first lockout is 30 seconds; each further failure doubles it, up to 15 minutes.
- A successful sign-in sets a *device cookie* (`stemtrace_session_device`, signed, kept on
  logout). A browser that sends a valid one for the username it signs in with gets its own
  limit of 5 failures and skips the IP limit (its failures still count against the IP). Every
  other browser shares a per-username limit of 20 failures, from all IPs together.
- Device cookies stay valid for 90 days only with a configured `STEMTRACE_LOGIN_SECRET`
  (`login_secret` / `--login-secret`). With a generated per-process secret they stop working
  on every restart and aren't recognized by other workers or replicas.
- The per-username limit can be held by anyone who knows the username: about one failed
  attempt every 15 minutes keeps sign-in locked, **indefinitely**, for every browser without a
  valid device cookie, e.g. a new laptop or a cleared browser. Browsers that signed in within
  the last 90 days are not affected. Restarting stemtrace clears all limits. If that's a
  concern, put stemtrace behind your own auth (see [Custom authentication](#custom-authentication))
  or a proxy with its own rate limits.
- Guessing costs the same for every username, real or not: 20 guesses, then one per lockout
  (about 4 an hour at the 15-minute cap), or, waiting out the hour after which failures are
  forgotten, another burst of 20. So the sustained maximum is about 20 guesses an hour per
  username. A count goes down only after an hour without failures or after a successful
  sign-in, which clears the counts for its IP, username and device. The response is the same
  whether the username exists or not.
- The client IP is the one your ASGI server reports; `X-Forwarded-For` is never read directly.
  Behind a reverse proxy or load balancer, tell the server which proxies to trust with uvicorn
  `--forwarded-allow-ips` or the `FORWARDED_ALLOW_IPS` env var (which `stemtrace server` also
  reads), listing their IPs or CIDRs, e.g. `FORWARDED_ALLOW_IPS=10.0.0.0/8`. Don't use `*` with
  a proxy that appends to `X-Forwarded-For`: uvicorn then takes the leftmost entry, which the
  client sets, so anyone can claim a new IP on every request. Without the setting, every user
  shares the proxy's address and its IP limit (browsers with a device cookie aren't held up by
  that). Requests without a client address (e.g. uvicorn `--uds`) have no IP limit; the
  username and device limits still apply.
- Limits are per process: with several workers or replicas, each counts on its own.
- Failures are counted in fixed tables of 65,536 buckets per kind (IP, username, device;
  about 1.5 MB each), not per key, so memory never grows and failure traffic can't push out or
  lower another key's count. Each key maps to two buckets through a hash keyed with a random
  per-process secret, and is locked only if both are. Keys that share buckets can therefore
  only be locked early, never late. (A successful sign-in clears its own two buckets, so with a
  probability of about 0.006% per sign-in it can also lift the lock of a key sharing both.)
  False locks for a username that never failed: about 0.02% with 500 usernames locked at
  once, 0.08% with 1,000, 2% with 5,000 and 21% with 20,000 (an attacker locking junk
  usernames on purpose); IPs behave the same. Junk-IP floods are cheap for anyone with many
  addresses (an IP locks after 5 failures, so 5,000 IPs take 25,000 requests) and lock out
  that share of other IPs, for browsers without a device cookie. Browsers with a device cookie
  are never affected by IP or username buckets.

Behind a reverse proxy that strips a path prefix, run your app with uvicorn's `--root-path`
(e.g. `uvicorn app:app --root-path /monitoring`); the login page, redirects and the session
cookie then use `/monitoring/stemtrace/...`.

### Built-in auth helpers (Basic / API key)

```python
import stemtrace

stemtrace.init_app(
    app,
    broker_url="redis://localhost:6379/0",
    auth_dependency=stemtrace.require_basic_auth("admin", "secret"),
)
```
