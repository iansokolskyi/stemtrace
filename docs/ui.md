# The dashboard

What each page of the stemtrace dashboard shows, which API endpoints feed it, and the limits you
can hit. Every page reads the same server state as the [API](features.md#summary-endpoints), so
a script can get any number you see here.

On this page:

- [The frame](#the-frame)
- [Overview](#overview)
- [Executions](#executions)
- [Flows](#flows)
- [Flow page](#flow-page)
- [Task page](#task-page)
- [Workers](#workers)
- [Worker page](#worker-page)
- [Registry](#registry)
- [Anomalies](#anomalies)
- [Sidebar badges](#sidebar-badges)
- [Command palette](#command-palette)

## The frame

Every page sits in the same frame:

- **Sidebar.** Links to Overview, Executions, Flows, Workers, Registry and Anomalies, with
  [badges](#sidebar-badges). Below 900 px wide, the sidebar is a drawer that you open with the menu
  button. When the server has [task groups](configuration.md#task-groups), the sidebar lists them,
  each with its failure rate, and lists their tags. A group or a tag opens
  [Executions](#executions) filtered by it. A group's failure rate is red above 1%. It covers the
  time range you picked. The sidebar footer shows the connection status, a theme switch, and the
  version. When you set up the built-in login, it also shows a **Logout** button.
- **Top strip.** The **Back** button, the breadcrumb, the [search button](#command-palette) and
  the time range switch.
- **Time range.** **1h**, **6h** and **All** appear on Overview and Executions only. The default
  is 6h and isn't written to the URL; the others are the `range` URL parameter. Links to
  Executions from the sidebar, the Overview and the other pages keep the range you picked. On
  Executions, dates you set in the filter replace the range.
- **Connection status.** **Live** means the browser has a WebSocket to the server, so the task
  and flow lists refresh on events. **Polling** means no WebSocket: those lists refresh every
  5 seconds. **Connecting**, **Offline** (the server is unreachable) and **Unauthorized** (your
  session was signed by a different server process) are the other states. The Overview, the sidebar
  badges, Registry and Anomalies refresh on their own timers, with or without a WebSocket.
- **Old links.** `/?state=FAILURE` and other task-list links at `/` open Executions with the same
  filters. `/graphs` opens Flows, and `/graph/<id>` opens `/flows/<id>`.

## Overview

The Overview answers "is something wrong right now?" for a time range. Its numbers refresh every
15 seconds, and its anomalies every 30 seconds.

**Data:** `GET /api/overview` (the tiles), `GET /api/stats/timeseries` (the chart, the sparklines
and the group trends), `GET /api/failures` (What's wrong), `GET /api/stats/tasks` (task health),
`GET /api/workers?refresh=false` (the worker strip and the Workers tile) and `GET /api/anomalies`
(the Anomalies tile and the anomaly rows).

### KPI tiles

Five tiles sit above the chart:

- **Runs / min**, **Failures** and **Failure rate** for the range, each with a sparkline. A run is
  a task that ended with SUCCESS, FAILURE, REVOKED or REJECTED. IGNORED tasks aren't runs. The
  failure rate is FAILURE runs divided by runs. See
  [Summary endpoints](features.md#summary-endpoints).
- A tile compares its value with the window of equal length immediately before it ("earlier: 2.4/min").
  The note says **In baseline**, or how far above or below it the value is. A tile is flagged at
  1.3 times its baseline (1.4 times for runs per minute, up or down) and flagged more strongly at
  2 times. When the baseline is 0, **Failures** and **Failure rate** say **Above baseline** for
  any value over 0, and **Runs / min** has no note. With **All**, the tiles show no comparison.
- **Workers online** shows online and known worker hosts and links to [Workers](#workers). It
  reads worker state without asking the workers.
- **Anomalies** counts the flagged items, by kind. It opens the list in the triage panel.

Clicking **Runs / min**, **Failures** or **Failure rate** opens the matching runs in the
[triage panel](#triage-panel). The **Failures** and **Failure rate** tiles list failures only, so
that list has no state chips.

### Runs chart

The chart stacks success, failure and retry runs per 5-minute bucket. With **All**, the server
picks the bucket size. Retry bars are hatched.

- Click a bar to open its runs in the triage panel.
- Drag across bars to select a span, then use **Open runs** to list it. The selection isn't kept
  in the URL.
- With the chart focused, **Left** and **Right** move between bars and **Enter** opens one.
- **Table** lists the same numbers as the chart.

### What's wrong

**What's wrong** lists **Failures by exception** (exception class, task, count, first seen and
last seen) and, beside or below it, the current anomalies. A row opens in the triage panel. When
there are neither failures nor anomalies, it says **All clear**.

### Worker strip

The strip shows a card for each worker host with its status, how many tasks it registered and when
the server last saw it. A card opens the worker in the triage panel.

### Task health

The table groups runs by [task group](configuration.md#task-groups), with runs, failure rate,
p50, p95, a trend and the last run. A group opens in the triage panel. Expand it to see its tasks,
and open a task to see its runs. A group row shows runs, failure rate, p95, a trend and the last
run; its p50 cell is empty, and with **All** it has no trend. A task row shows its p50 and has no trend. A group whose failure rate
is over 1% shows the rate in red and a red trend. A task over 1% is tinted, bold and has an icon,
so color isn't the only signal.

### Triage panel

The triage panel opens over the Overview from the right (a full-screen sheet below 900 px) and
doesn't move the page. It holds a stack of entries: runs of a bar, a span, a tile or a task name; an
exception class; an anomaly; a worker; a task group; and a task. A row in an entry opens
the next entry on top of it.

- **Esc** or the breadcrumb goes back one level. **×** closes the panel. **Expand** and
  **Collapse** change its width.
- The stack is the `panel` URL parameter, so a link, **Back** and a reload restore it.
- A runs entry shows the counts of its span and a list of runs that you narrow with state chips
  (failure, retry, started, pending and success), except for the failure tiles. A task entry links to the task page and
  to its flow.

### Limits

- The Overview counts the tasks the server holds. Tasks that the server evicted aren't in any
  number. See [Event retention](retention.md).
- **Workers online** and the worker strip show no pool size or active slots, because the API
  doesn't carry them.
- A failed request leaves its part of the page empty. The Anomalies tile says **Unavailable** when
  `GET /api/anomalies` fails.

## Executions

Every run the server holds, newest update first, as a table that loads 200 rows at a time. A
row opens the [task page](#task-page). The heading counts the tasks that match the filters and
the range, finished or not, and calls them runs, for example "1,204 runs in the last 6h".

**Data:** `GET /api/tasks` with `compact=1`, `limit=200`, the filters and `within` (the **List**
view), and `GET /api/tasks` with `limit=50` (the **Timeline** view). The groups for suggestions come from
`GET /api/groups`.

### Filters

Filter chips narrow the list. Each chip opens a small editor:

- **State** (one or more of the nine Celery states), **Name** (part of a task name) and **Task**
  (the exact name).
- **Exception**, **Worker**, **Queue**, **Group** and **Tag**. The editors suggest values from the
  rows that are already loaded and from the task groups.
- **Time** takes **Today**, **Yesterday**, **Last 7 days**, **Last 30 days**, or **From** and
  **To** dates. Dates replace the time range.

Filters are in the URL, so you can share a view. **Clear all** removes the filters and keeps the
range.

### Columns and rows

- **State**, **Task** and **Started** always show. **Group**, **Worker**, **Queue**,
  **Queue wait**, **Runtime** and **Anomaly** show only when a row on the first page has a value.
  The table decides the columns once for a set of filters, from the first page. Scrolling and
  refreshing don't change them.
- A row with an anomaly is tinted and has an anomaly flag. See [Anomalies](#anomalies).
- **Compact** and **Comfortable** set the row height.
- While your pointer is over the table, or you scrolled away from its top, rows keep their order.
  Tasks that would appear above them wait behind an **N new** button. A row that no longer matches
  your filters stays, dimmed, with its last known data, until you release the table.

### Timeline view

**Timeline** draws the latest 50 tasks that match the filters as bars from their first event to
their last update. A bar opens the task. The view doesn't page.

### Limits

- Only tasks the server still holds appear. See [Event retention](retention.md).
- The **Worker** and **Queue** columns show what events carry. See
  [Worker, queue and exception type](features.md#worker-queue-and-exception-type).

## Flows

One row for each workflow that the server holds: a root task and everything it started. A row shows
the worst state in the flow, the root's short name, a summary such as "50 tasks, 1 failed, chord
callback not run", the root's full name and when the flow started. A row opens the
[flow page](#flow-page). The list loads 50 flows at a time as you scroll.

**Data:** `GET /api/graphs?limit=50`. The `flow` field of each item holds the summary. See
[Graphs](graphs.md#canvas-graph-visualization).

### Limits

- The time range switch doesn't apply to Flows. The page reads the `from_date` and `to_date` URL
  parameters, but has no control to set them.

## Flow page

One flow as a diagram and a timeline.

**Data:** `GET /api/graphs/{root_id}`. The inspector also reads `GET /api/tasks/{task_id}` for the
exception and keyword arguments of the task you chose. The page works out the summary line
itself, because this endpoint doesn't send `flow`.

- **Header.** The root's short name, the flow's worst state and the summary. **Jump to failure**
  chooses the first task that failed and scrolls to it (it's disabled when no task failed). The
  zoom buttons step from 100% to 140%, and **Fit** shows the diagram at its default size.
- **Diagram.** Tasks run left to right. The tasks that a task started stack to its right. A group
  or chord is a container with one track for each member. A chord's callback follows the
  container. When a chord's callback didn't run, a box says why, for example
  "Chord header failed: 1 of 48 tasks failed". A container of more than 12 members shows the first
  3, the failed ones and a **+N more** block; select the block to expand it. A task block shows its
  state as an icon, its name, and a second line with the exception class of a failure and the
  runtime (a task with no runtime yet says "running", "pending" or "received"). A member of a
  container shows its icon, its name and its runtime. When every member of a container is the same
  task, the container's title names the task ("Group of 5 send_email") and each member shows the
  first 8 characters of its task ID instead of the name. A screen reader hears the state word as
  well.
- **Inspector.** The task you chose (the first failure, or the root, when you haven't chosen):
  name, ID with a copy button, worker, queue, queue wait, runtime, exception, up to four keyword
  arguments, and **Open full detail**. For a group or chord: members, callback and duration.
- **Timeline.** A row for each task on a shared time axis. A grey lead-in is the queue wait, a
  dotted lead-in is a countdown or ETA, and an outlined bar is on the critical path. Bars of
  running tasks grow every second. A collapsed container is one row for its hidden members. A bar
  that runs past the end of the axis is cut with a slash and
  says how long it has run. A row chooses its task and scrolls the diagram to it.

While the flow is running, new tasks scroll into view until you scroll, zoom, choose a task, or
expand a container. After that, live updates don't move the view.

### Limits

- The Inspector shows four keyword arguments. The [task page](#task-page) shows all of them.
- How stemtrace draws chains, groups, chords and `replace()`, and when it can't, is in
  [Graphs](graphs.md), including the [known limitations](graphs.md#known-limitations).

## Task page

One task, with its history.

**Data:** `GET /api/tasks/{task_id}`, which returns the task with its events, its child
tasks, the root of its flow and, for a flagged task, `anomaly_detail`.

- **Header.** The module path above the short name, the state, the anomaly flag when there is one,
  and the ID with a copy button. **Open flow** shows when the task is part of a larger flow,
  **Worker** opens Executions for its worker, and **All runs of this task** opens Executions
  for its exact name.
- **Anomaly callout.** For a [flagged](#anomalies) task: why, since when, and links onward.
- **Timing.** Queue wait, then runtime, on one scale. It shows the parts the server knows.
- **Execution timeline.** One bar with a segment for each event, sized by how long the task stayed
  in that state, and the same segments listed below. It needs two or more events.
- **Exception.** The type and message of the last failure or retry, with the traceback behind a
  toggle.
- **Result**, **Output** and **Arguments.** What the task returned and, when you
  [capture it](configuration.md#task-output-print-and-logging), what each attempt printed and
  logged. The task's arguments are those of its first events that carry them. Values are the ones
  the server [scrubbed](security.md).
- **Details.** Group, tags, worker, queue, routing key, retries, ETA, first seen, last update and
  duration. A fact without a value isn't listed.
- **Event history.** Every event in time order, with Celery's own event names.
- **Child tasks.** The tasks this one started, as links.

A section with nothing to show (no exception, no output, no children) isn't drawn.

### Limits

- A task that the server evicted is gone: the page says **Failed to load task**.
- The timing fields come from the events the server holds. See
  [Queue wait and runtime](features.md#queue-wait-and-runtime).

## Workers

One row for each worker host, with its status, how many tasks it registered, when it started and
when the server last saw it. A host is **Online** when one of its processes is online. An
offline host is highlighted. The heading says "2 of 3 online". A row opens the
[worker page](#worker-page).

**Data:** `GET /api/workers`. The server asks the workers with Celery inspect before it answers,
so the list is current when the page loads.

### Limits

- The page reads the list when it loads. It reloads every 30 seconds whenever the live
  connection (the WebSocket) isn't connected, and never while it is connected, so a worker that changes while you watch
  doesn't change on the page until you open it again.
- Until an inspect reply names a new worker, the server can show it offline. The rules for when
  the server asks, how a restarted node is handled and when a silent worker goes offline are in
  [Workers and registry inspect](features.md#workers-and-registry-inspect).
- Pool size and active slots aren't in the API, so the dashboard doesn't show them.

## Worker page

One host.

**Data:** `GET /api/workers` (the page picks its host from the list, so it asks the workers like
Workers does, and refreshes under the same rule: every 30 seconds whenever the live connection (the
WebSocket) isn't connected, never while it is connected) and `GET /api/tasks?hostname=<host>&limit=25&compact=1`.

- **Header.** The hostname and status, a note with the last-seen time for an offline host, and
  **All executions on this worker**.
- **Registered tasks.** The tasks any process of the host registered, each linking to Executions
  for the exact name, with a group column when a task has a group.
- **Recent executions.** The latest 25 runs on the host, with the same columns as
  [Executions](#executions) except **Worker**.
- **Details.** PID, start time and last-seen time of the process that the page describes.

When a host has several processes (a restart leaves the old process listed for a while), an
online process describes the host; otherwise the one seen last does.

## Registry

Every task name that a worker registers or that the server has seen run, with its status and its
numbers for the last 6 hours.

**Data:** `GET /api/tasks/registry` and `GET /api/stats/tasks?within=6h`. The first load of the
page asks the workers (inspect). The page then reloads every 30 seconds without asking them.
A failed stats request leaves the number columns empty.

| Status | Meaning |
|--------|---------|
| **Active** | A worker registers the name and the server has seen it run. |
| **Never run** | A worker registers the name and the server hasn't seen it run. The note names the workers. |
| **Not registered** | The server has seen the name run, but no worker registers it. New runs with this name can stay in PENDING. |

- **Columns.** Task, group (only when some task has a group other than `ungrouped`), status, runs
  (6 h), **Fail %**, p50, p95 and last run. A failure rate over 1% is bold. A row opens Executions
  for the exact name.
- **Search.** The box filters on task name, group and status, and every word you type must match.
- The registry hides Celery's built-in `celery.*` tasks.

### Limits

- The numbers always cover 6 hours, whatever the top strip says (the switch isn't on this page).
- A task that no current worker registers can stay PENDING. [Anomalies](#anomalies) flags it as
  unregistered under stricter rules, which [Unregistered](features.md#unregistered) lists.

## Anomalies

The tasks that need attention, found without reading every row. The heading says how many are
active.

**Data:** `GET /api/anomalies?limit=500`. The server asks the workers first, and the page reloads
every 30 seconds. A task has at most one anomaly.

| Kind | What the row says | Links |
|------|-------------------|-------|
| **Stuck** | No result seen for a time, and the limit for this task. With enough finished runs, the limit comes from the task's p95 and the row names it. | The task, its worker, all runs |
| **Interrupted** | The worker shut down, was terminated or restarted while the task ran, or Celery cancelled the task. | The task, its worker, all runs |
| **Unregistered** | N tasks are PENDING and no online worker registers the name. When a registered name is close to it, the row asks "renamed from `<name>`?". | The Registry, all runs |
| **No workers online** | N tasks are waiting and no worker is online. It's one banner for the fleet, not one row for each name. | Workers, the waiting tasks |

The rules for each kind (the thresholds, the evidence, when the registry is trusted and how a
similar name is chosen) are in
[Stuck, interrupted and unregistered tasks](features.md#stuck-interrupted-and-unregistered-tasks).
The same flags show elsewhere: the **Anomaly** column of [Executions](#executions), the callout of
the [task page](#task-page), and the Overview tile and [What's wrong](#whats-wrong).

### Limits

- The page shows the first 500 items and says "Showing 500 of N" when there are more.
- A request that takes more than 10 seconds shows "Anomalies are taking a long time to load" and
  keeps the last result. On a slow broker that is healthy, the sidebar badge and the Overview
  tile can time out the same way.
- The server computes anomalies when a request asks. They don't arrive on the WebSocket, and the
  page asks every 30 seconds, so a flag appears at the next request.
- Every limit of the rules applies: skewed clocks, killed workers, older workers and a
  [broker URL that needs a working inspect](features.md#limitations).
  [No workers online](features.md#limitations-of-no-workers-online) has its own limits.

## Sidebar badges

Three badges flag problems on every page:

| Badge | Counts | Data |
|-------|--------|------|
| **Workers** `N offline` | Hosts with no online process | `GET /api/workers?refresh=false` |
| **Registry** `N not registered` | Names with status **Not registered** | `GET /api/tasks/registry?refresh=false` |
| **Anomalies** `N` | Anomaly items, the fleet item included. A name with 40 waiting tasks counts once. | `GET /api/anomalies` |

Each badge refreshes every 30 seconds and is hidden at 0 or while unknown.

### Limits

- The **Workers** and **Registry** badges never ask the workers. They show what the server already
  knows, so they can lag until you open the page, which does ask.
- Every request of the **Anomalies** badge asks the server to inspect the workers. The server
  paces the inspects, but a request can still wait for one: about 5
  seconds with the broker down, up to 7 seconds when it joins an inspect that is running.

## Command palette

**Ctrl+K** (**⌘K** on macOS) opens a search from any page. The search button in the top strip opens
it too. The shortcut also works on a layout whose K key types non-Latin letters, such as Ukrainian,
because it reads the physical key.

- Type a **task ID** (8 or more hexadecimal digits and dashes) to find that task. A shorter
  text is searched as a name.
- Type part of a **task name**, a **flow** title or a **worker** hostname. The match is fuzzy, so
  `snd` finds `send_email`. Results are grouped: up to 6 task names, 4 flows and 5
  workers.
- With nothing typed, the palette lists flows and workers.
- **Up** and **Down** move, **Enter** opens, **Esc** closes. A task ID opens the task page, a name
  opens Executions for it, a flow opens the flow page and a worker opens the worker page.

**Data:** the palette uses `GET /api/tasks/registry?refresh=false`,
`GET /api/graphs?limit=50` (the first 50 flows) and `GET /api/workers?refresh=false`. The first
and last are the same cached results that the sidebar badges use. The palette requests each one
again only when its result is older than 5 seconds. A task ID is
looked up with `GET /api/tasks/{task_id}`.

### Limits

- Only the first 50 flows are searched.
- The palette never asks the workers, so a worker or a task name that the server hasn't learned
  yet isn't in the results.
