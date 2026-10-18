---
name: stemtrace-api
description: Answer questions about Celery tasks monitored by stemtrace ("what failed in the last hour and why", "is this workflow stuck", "which workers are up") by querying the stemtrace JSON API with curl and jq. Use when a project runs stemtrace (stemtrace server, or stemtrace.init_app in a FastAPI app) and you need task failures, exceptions, tracebacks, workflow graphs, workers or the task registry.
---

# Querying stemtrace from the command line

stemtrace keeps recent Celery task events in memory and serves them as JSON. Everything below
uses `curl` and `jq`. For totals rather than single tasks, start with the summary endpoints
(`overview`, `failures`, `stats/timeseries`); then drill into tasks.

## Setup

```bash
# stemtrace server: http://HOST:8000/stemtrace ; init_app(app): your app's URL + the prefix
# (default /stemtrace)
export STEMTRACE_URL="http://localhost:8000/stemtrace"
# The key the server was started with, if any. Not STEMTRACE_API_KEY: in that name it would also
# turn the key on for a stemtrace server or app you start from this shell.
export ST_API_KEY="..."

st() {  # st PATH [curl args...]: GET $STEMTRACE_URL/api/PATH as JSON
  local path="$1"; shift
  curl -sS --fail-with-body -G \
    ${ST_API_KEY:+-H "Authorization: Bearer $ST_API_KEY"} \
    "$STEMTRACE_URL/api/$path" "$@"
}

st health | jq .   # {"status":"ok", "consumer_connected": true, "node_count": ...}
```

Auth: when the server runs with an API key (`STEMTRACE_API_KEY` / `--api-key`), send
`Authorization: Bearer <key>`. Without it you get `401` with `WWW-Authenticate: Bearer`. A wrong
key five times from one IP gives `429` with `Retry-After` (seconds): wait, don't retry in a loop.
A `401` from a server without a key means it uses a browser login or a custom auth dependency:
ask the user for credentials. `GET /api/health/live` never needs the API key.

## What failed in the last hour, and why

```bash
SINCE=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)   # macOS: date -u -v-1H +%Y-%m-%dT%H:%M:%SZ

# Failed tasks, newest first: id, name, when, worker, exception class, message
st tasks --data-urlencode "state=FAILURE" --data-urlencode "from_date=$SINCE" \
  --data-urlencode "limit=100" |
  jq -r '.total as $t | "\($t) failed", (.tasks[] | [.task_id, .name, .last_updated,
    (.hostname // "-"), (.exception_type // "-"),
    ([.events[] | select(.state == "FAILURE") | .exception] | last // "-")] | @tsv)'

# Failures grouped by exception class, computed by the server (count, class, task names)
st failures --data-urlencode "within=1h" |
  jq -r '.groups[] | [.count, (.exception_type // "-"), (.task_names | join(","))] | @tsv'

# Runs (SUCCESS/FAILURE/REVOKED/REJECTED, not IGNORED), failure rate and workers for the
# last hour, against the hour before
st overview --data-urlencode "within=1h" |
  jq '{runs, failure_rate, workers}'

# Count failures by exception class and task
st tasks --data-urlencode "state=FAILURE" --data-urlencode "from_date=$SINCE" \
  --data-urlencode "limit=500" |
  jq -r '[.tasks[] | "\(.exception_type // "?")\t\(.name)"] | group_by(.)
    | map("\(length)\t\(.[0])") | sort_by(-(split("\t")[0] | tonumber)) | .[]'

# Every failure of one exception class
st tasks --data-urlencode "state=FAILURE" --data-urlencode "exception_type=myapp.errors.PartnerAPIError" |
  jq -r '.tasks[] | [.task_id, .name, .last_updated] | @tsv'
```

Then the full story of one task (traceback, arguments, retries, captured output):

```bash
TASK_ID=...
st "tasks/$TASK_ID" | jq '{name: .task.name, state: .task.state, duration_ms: .task.duration_ms,
  events: [.task.events[] | {state, timestamp, retries, args, kwargs, exception}],
  traceback: ([.task.events[] | select(.traceback) | .traceback] | last),
  children: [.children[] | {task_id, name, state}]}'
```

`exception` and `traceback` live on the task's **events**, not on the task: look at the
`FAILURE` event (a `RETRY` event carries the exception of a failed attempt that was retried).
`output` (captured print/logging, if the workers enable `capture_output`) is only included on
this detail endpoint.

## The workflow a task belongs to

```bash
# The root of the task's workflow (a root task has no root_id or its own id)
ROOT=$(st "tasks/$TASK_ID" | jq -r --arg id "$TASK_ID" \
  '[.task.events[].root_id | select(.)] | first // $id')

st "graphs/$ROOT" | jq -r '.nodes[] | [.task_id, .node_type, .name, .state,
  (.duration_ms // "-"), (.children | length)] | @tsv'
```

`/api/graphs/{id}` returns the subgraph from that node down, so pass the root for the whole
workflow. Nodes with `node_type` `GROUP` or `CHORD` are synthetic containers (ids like
`group:<id>`); their tasks are in `children`. `parent_id` is where a node sits in the graph;
`reported_parent_id` is the parent Celery reported.

Recent workflows: `st graphs --data-urlencode "limit=20" | jq -r '.graphs[] | [.task_id, .name,
.state, .last_updated] | @tsv'`.

## Workers and the task registry

```bash
st workers | jq -r '.workers[] | [.hostname, .pid, .status, .last_seen] | @tsv'

# Tasks that ran but no online worker registers (often a deploy mismatch)
st tasks/registry --data-urlencode "status=not_registered" |
  jq -r '.tasks[] | [.name, .execution_count, .last_run] | @tsv'
```

Registry `status` is `active` (ran and registered), `never_run` or `not_registered`.

## Filters and pitfalls

- **States are uppercase** in responses: `PENDING RECEIVED STARTED SUCCESS FAILURE REVOKED REJECTED RETRY IGNORED`.
  `state=` takes one state or a comma-separated list (`state=FAILURE,RETRY`), in any case. An
  unknown state is a `422` that lists the valid ones.
- `within=1h` (`30s`, `15m`, `6h`, `7d`) is a shorter way to say "since an hour ago" than
  `from_date`, on tasks, graphs and the summary endpoints; pass one or the other (the overview
  takes `within` only).
- `state` is the task's **current** state: a task that failed and then succeeded on retry is
  `SUCCESS`; one waiting to retry is `RETRY`.
- `name` is a case-insensitive substring (`name=charge` matches `billing.charge_card`);
  `name_exact` is the whole name, case-sensitive. `hostname` (the worker's nodename, as the
  workers endpoint lists it), `queue` and `exception_type` match exactly.
- `exception_type` is the class only (`ValueError`, `myapp.errors.PartnerAPIError`), never the
  message; the message is in the events' `exception`. A task's `exception_type` is the last
  exception it raised, also when a retry then succeeded, so add `state=FAILURE` for failures.
  It is missing on events from workers older than the server.
- `from_date` keeps tasks **updated** at or after it; `to_date` keeps tasks **first seen** at or
  before it, and a date-only or midnight `to_date` means the end of that day. Use ISO 8601 in
  UTC with `Z`. URL-encode it (`--data-urlencode`): a raw `+01:00` arrives as a space
  and the request fails with `422`.
- Pagination: `limit` (tasks: 1 to 500, default 100; graphs: 1 to 100, default 50) and `offset`;
  `total` is the count before paging. Results are newest first (by last update), so new events
  shift pages: page with a fixed `to_date`, or re-check `total`.
- Out-of-range `limit` gives `422`; an unknown task or graph id gives `404`.
- History is in memory and bounded (default 10,000 tasks; events also expire on the broker).
  "No failures" means none in what the server holds, not none ever. `/api/health` shows
  `node_count`, and `consumer_connected: false` means the server isn't receiving events.
- `tasks/registry` and `workers` ask the workers over the broker (up to about half a second)
  unless you pass `refresh=false`.
- The task list leaves out `output`; fetch `tasks/{id}` for it.
