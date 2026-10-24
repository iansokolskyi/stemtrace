# Graphs

stemtrace draws each workflow as a graph: tasks are nodes, a parent task points to the tasks it
started, and Celery canvas constructs (groups and chords) are drawn as containers.

## Canvas graph visualization

`stemtrace` automatically detects and visualizes Celery canvas constructs:

```text
# Parent-spawned group: GROUP is child of parent
batch_processor
└── ┌─ GROUP ──────────┐
    │  ├── add(1, 2)   │
    │  ├── add(3, 4)   │
    │  └── add(5, 6)   │
    └──────────────────┘

# Standalone group: GROUP is a root node
┌─ GROUP ──────────┐
│  ├── add(1, 1)   │
│  ├── add(2, 2)   │
│  └── add(3, 3)   │
└──────────────────┘

# Chord: header tasks inside, callback outside with edges
┌─ CHORD ──────────┐
│  ├── add(10, 10) │──┐
│  ├── add(20, 20) │──┼──► aggregate_results
│  └── add(30, 30) │──┘
└──────────────────┘

# Group of chains: each chain is a row inside the group
┌─ GROUP ─────────────────────────┐
│  fetch(1) ──► parse(1)          │
│  fetch(2) ──► parse(2)          │
└─────────────────────────────────┘
```

- **Synthetic containers**: GROUP/CHORD nodes are always created when 2+ tasks share a `group_id`
- **Parent linking**: When spawned from a parent task, the container becomes a child of that parent
- **Chord callbacks**: Rendered outside the container with edges from each header task. The callback is the task the chord's body starts with (for a chain body, the rest of the chain follows it). It is drawn only when IDs in the events name it, so a chord shows its real callback or none, never another task. See [Chord callbacks](#chord-callbacks). A chord whose body never ran (e.g. a header failed) shows no callback, and a header's own subtasks are never mistaken for it. In `GET /stemtrace/api/graphs/{root_id}`, such a CHORD node says why in `not_run_reason`: `header_failed` when a header failed or was rejected, `header_revoked` when a header was revoked (and none failed), `header_ignored` when a header raised `Ignore` (and none failed or was revoked). A header is a task whose events name the chord's callback or the first task of its body; for a header that is a chain, Celery puts the chord only on the chain's last task, so that task is the header. Celery checks the header results with `propagate=True`, so the callback can't run after the first failed or revoked header, and the reason is set then, even while other headers still run (a rejected header never reports its part, so the chord never completes; workers report a rejection only when the message won't run again; an ignored header doesn't report its part either, unless the task reported its own result with `backend.mark_as_done` first, and then the callback runs and the reason clears). As headers end it can only move up, from `header_ignored` to `header_revoked` to `header_failed`, and it clears only when a callback is drawn (as just said). It is worked out from the headers still stored: the store evicts a chord whole, with the rest of its unit, but when the unit is too big to keep (over 90% of `max_nodes`) its oldest nodes go, which can lower or clear it. It is empty while no header has failed, been revoked or been ignored, when a callback is drawn, and in the `/graphs` list (which doesn't hold every header)
- **Replaced tasks**: `task.replace()` makes Celery reuse the replaced task's ID for the replacement's last task, so one graph node stands for both. It stays where the original task was sent: its name and parent come from its own events, not the replacement's (which report a task below it as parent, its child or, for a longer chain, a task further down; or, when the replacement is a single task, the task itself), whatever the hosts' clocks say; the replacement's other tasks hang from it, and a callback that reuses the ID can't also be drawn after a chord. The same holds when one of the replacement's tasks is replaced in turn (a nested `replace()`, up to 16 levels deep), and for a task that keeps replacing itself (recursive pagination: every page's tasks hang from one node). A server that starts after the replaced task ran only sees the replacement's events: the node shows the replacement's name. A replaced workflow root stays the root, with the replacement's other tasks under it. A replaced task inside a workflow loses its link to its parent and forms a graph of its own with the replacement's tasks. After a two-task replacement, it and the replacement's first task name each other as parent, and the one with the larger task ID is on top; after a longer replacement chain they name each other in a loop, and which of them is on top depends on the order events arrive in
- **Chains in groups**: For `group(chain(a1, b1), chain(a2, b2))` Celery only marks `b1` and `b2` as group members. A member's parent task whose only child is that member, and which belongs to no group and isn't a chord callback, is drawn inside the group as part of the member's chain (and so on up the chain), so the whole workflow is one graph. A parent with other children (e.g. one that started the group, or spawned other tasks) is never pulled in. Chains are drawn once every member has reported its parent. See [Chord callbacks](#chord-callbacks) for a chord whose body is a group
- **Failed chains**: When a chain step fails, Celery never runs the steps after it and sends no event for them. stemtrace publishes a `FAILURE` for each of those steps, from the worker that ran the failed one (one publish per step), so a failed chain shows its steps up to the first group or chord in it, the unrun ones failed with the same exception and no worker. When the chain is a chord header, its last step is the header, so the chord shows it failed. A group or chord in the chain isn't a task: it and every step after it (Celery moves them into the chord) aren't shown. A step that fails after Celery already sent the next one (its result couldn't be stored) doesn't fail the steps after it: they run, on any supported Celery version. When a chain step uses `task.replace()` and the replacement fails, no FAILURE is published for the steps after it, so they aren't shown (as before this change). A revoked step is different: Celery doesn't record the steps after it, so they don't appear, and a chord they belong to never completes. Failures the worker's main process records (a hard time limit, a lost worker) publish the later steps too. A failure that lands after the step's body returned (for example a hard time limit that hits in its `on_success` handler) behaves differently when the step already sent the next one, which the main process can't see. Every later step then shows `FAILURE`: a step that already finished keeps `FAILURE` (its later `FAILURE` wins over its `SUCCESS`), and a step still running or not yet sent shows `FAILURE` until its own result arrives, then `SUCCESS`. A step not yet sent can show `FAILURE` before its `PENDING`. With Redis and other key-value result backends, the result backend doesn't agree for the steps that already finished: it keeps their `SUCCESS` (it never overwrites a stored `SUCCESS`), and the step killed in `on_success` stays `SUCCESS` there too. With a database result backend (`db+...`) it records `FAILURE` for them, as stemtrace shows. A failure during the step's body doesn't do this. When a hard time limit hits in a step's own `on_success` handler, the step shows `FAILURE` from the main process, then `SUCCESS` from its pool process just after, so it ends `SUCCESS`. With Redis and other key-value result backends, the result backend keeps `SUCCESS` too. With a database result backend (`db+...`) it records `FAILURE` for that step, so it disagrees with the `SUCCESS` stemtrace shows. Other result backends aren't checked. The stemtrace behavior is checked with Celery 5.6.3, a prefork pool and Redis. The two backend behaviors are checked with Celery 5.6.3 (the key-value guard and the database update are also in 5.2.7's source).
- **Timing**: Each node displays start time and duration directly in the graph
- **Aggregate state**: Container shows running/success/failure based on member states

To label nodes with a task argument instead of the task name, see
[Node names from task arguments](configuration.md#node-names-from-task-arguments).

## Chord callbacks

stemtrace draws a chord's callback, the task its body starts with, only when IDs in the events name it.
It reads no parent, timestamp or task name (except to recognize `celery.chord_unlock`) and not
the order events arrive in. A chord shows its real callback or none, never another task, whichever
events arrive late or are lost. Tests replay chords recorded from Celery 5.2.2 and 5.6.3 with
a result backend that joins chords natively (cache and Redis) and with one that uses
`celery.chord_unlock` (a database backend), and check every chord after every event, after any one
event is lost and after any one task is evicted.

A chord is the group whose header tasks carry the group's `group_id` and a `chord_callback_id`
(the callback ID Celery stamps on them) or a `chord_body_first_id` (the ID of the first task of
the body, from workers 0.4.3 on). Celery 5.2 puts neither on the headers of a chord with a chain
body. For such a group a `celery.chord_unlock` task makes the chord: the members its `result`
lists are the headers.

stemtrace picks the callback in this order:

1. **The first task of the body.** Header tasks report it as `chord_body_first_id` when the body
   is a task, or a chain that starts with a task. A `celery.chord_unlock` task names it in its
   arguments (the group ID and the body signature) when the result backend has no native chord
   support. If both name one, they must agree. The task must have events and must not be a header.
2. **The stamped callback.** The `chord_callback_id` the headers name counts when it is a task that
   isn't a member of the chord's group, or a group other than the chord's own that has tasks
   outside the headers (the inner chord of a chord body, or a Celery 5.2 group body). It must also
   have been reported by a worker, not only announced by its sender, and no other chord may name it.
   Celery 5.6 gives a task body's callback and a chain body's last task the chord's group ID, so
   a stamped task that carries it could be either: it isn't drawn.

A chord draws no callback when:

- the evidence conflicts: the headers name different first tasks or stamps, a `celery.chord_unlock`
  task and the headers name different first tasks, or two `celery.chord_unlock` tasks do. A
  `celery.chord_unlock` task whose `result` doesn't list every header is not the chord's;
- two chords name the same task;
- the body starts with a chord or a group, or is a group on Celery 5.6, which stamps the chord's own
  group ID for it;
- the body is a chord on Celery 5.2, which stamps an ID no event carries;
- the callback hangs above the chord. `task.replace()` into a group or chord makes the replaced task
  that sent the headers the callback too, and a task can't be both above and below the chord;
- the callback hasn't run yet, or its events haven't arrived.

| Body of the chord | Celery 5.6 | Celery 5.2 |
|---|---|---|
| A task | Drawn from `chord_body_first_id`. Without it, drawn when the callback doesn't carry the chord's group ID | Drawn |
| A chain that starts with a task | Drawn from `chord_body_first_id` or the `celery.chord_unlock` task | Drawn from `chord_body_first_id` or the `celery.chord_unlock` task |
| A chord | Drawn (the inner chord) | Not drawn |
| A group | Not drawn | Drawn when its tasks carry the group ID the headers name |
| A chain that starts with a chord or group | Not drawn | Not drawn |

### Upgrading workers

Workers before 0.4.3 don't report `chord_body_first_id`. The server draws what it can from their
events: a chain body's callback, and on Celery 5.6 a task body's callback that carries the chord's
group ID, aren't drawn until you upgrade the workers. The body's tasks then hang from the header
they report as their parent. A chord that has no callback drawn keeps its `not_run_reason`, so a
chord whose header was ignored after it reported its result shows `header_ignored` though its
callback ran. A server from before the field ignores it: upgrade the server and the workers in
either order.

A `celery.chord_unlock` task is tied to its chord by its arguments. With `capture_args=False`, or
arguments over the 10 KB limit, it isn't, and it shows as a graph of its own; a chord with
`chord_body_first_id` still draws its callback.
