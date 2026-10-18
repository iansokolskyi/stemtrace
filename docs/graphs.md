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
- **Chord callbacks**: Rendered outside the container with edges from each header task (verified with Celery 5.6; on Celery 5.2 a chord whose body is a chain, or `chain(group(...), task)`, carries no chord callback ID and shows as a group, with the chain hanging off the header that finished last, inside the group). The callback is the task the chord's body starts with (for a chain body, the rest of the chain follows it). It is only drawn on evidence from the events: the callback the headers name, or the chord's group ID that Celery stamps on a chain body's last task, traced back to a header (or Celery's `chord_unlock` task). A chord whose body never ran (e.g. a header failed) shows no callback, and a header's own subtasks are never mistaken for it. In `GET /stemtrace/api/graphs/{root_id}`, such a CHORD node says why in `not_run_reason`: `header_failed` when a header failed or was rejected, `header_revoked` when a header was revoked (and none failed), `header_ignored` when a header raised `Ignore` (and none failed or was revoked). A header is a task whose events name the chord's callback; for a header that is a chain, Celery puts the chord only on the chain's last task, so that task is the header. Celery checks the header results with `propagate=True`, so the callback can't run after the first failed or revoked header, and the reason is set then, even while other headers still run (a rejected header never reports its part, so the chord never completes; workers report a rejection only when the message won't run again; an ignored header doesn't report its part either, unless the task reported its own result with `backend.mark_as_done` first, and then the callback runs and the reason clears). It never clears while events arrive (except as just said), but it can move up from `header_ignored` to `header_revoked` to `header_failed` as other headers end. It is empty while no header has failed, been revoked or been ignored, when a callback is drawn, in the `/graphs` list (which doesn't hold every header), and once the failed header has been evicted (`max_nodes`) while the chord is still stored
- **Replaced tasks**: `task.replace()` makes Celery reuse the replaced task's ID for the replacement's last task, so one graph node stands for both. It stays where the original task was sent: its name and parent come from its own events, not the replacement's (which report a task below it as parent, its child or, for a longer chain, a task further down; or, when the replacement is a single task, the task itself), whatever the hosts' clocks say; the replacement's other tasks hang from it, and a callback that reuses the ID can't also be drawn after a chord. The same holds when one of the replacement's tasks is replaced in turn (a nested `replace()`, up to 16 levels deep), and for a task that keeps replacing itself (recursive pagination: every page's tasks hang from one node). A server that starts after the replaced task ran only sees the replacement's events: the node shows the replacement's name. A replaced workflow root stays the root, with the replacement's other tasks under it. A replaced task inside a workflow loses its link to its parent and forms a graph of its own with the replacement's tasks. After a two-task replacement, it and the replacement's first task name each other as parent, and the one with the larger task ID is on top; after a longer replacement chain they name each other in a loop, and which of them is on top depends on the order events arrive in
- **Chains in groups**: For `group(chain(a1, b1), chain(a2, b2))` Celery only marks `b1` and `b2` as group members. A member's parent task whose only child is that member, and which belongs to no group and isn't a chord callback, is drawn inside the group as part of the member's chain (and so on up the chain), so the whole workflow is one graph. A parent with other children (e.g. one that started the group, or spawned other tasks) is never pulled in. Chains are drawn once every member has reported its parent. A chord whose body is a group (`chord([a, b], group(c, d))`) draws that group after the chord
- **Limitation: shared chord tasks**: Celery workflows don't produce events in which two chords share one `celery.chord_unlock` task or one task that starts a group body, two chords name the same callback, or one chord's headers name different callbacks. If such events arrive anyway (hand-crafted or malformed), the graph stays consistent, but which chord draws the shared task, and whether a chord's body shows as empty, can depend on the order the events arrive in. For callbacks the last name to arrive wins, and removing the task that named it (or evicting it) doesn't bring back a name another header or chord gave earlier.
- **Timing**: Each node displays start time and duration directly in the graph
- **Aggregate state**: Container shows running/success/failure based on member states

To label nodes with a task argument instead of the task name, see
[Node names from task arguments](configuration.md#node-names-from-task-arguments).
