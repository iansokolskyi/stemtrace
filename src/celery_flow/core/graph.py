"""Task graph models for representing task execution flows."""

from pydantic import BaseModel, ConfigDict, Field

from celery_flow.core.events import TaskEvent, TaskState


class TaskNode(BaseModel):
    """Mutable node in the task graph. Tracks event history and child relationships."""

    model_config = ConfigDict(validate_assignment=True)

    task_id: str
    name: str
    state: TaskState
    events: list[TaskEvent] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)
    parent_id: str | None = None


class TaskGraph(BaseModel):
    """DAG of task executions, built incrementally from events.

    Parent-child linking only occurs when parent exists at child insertion time.
    Out-of-order events won't back-link.
    """

    model_config = ConfigDict(validate_assignment=True)

    nodes: dict[str, TaskNode] = Field(default_factory=dict)
    root_ids: list[str] = Field(default_factory=list)

    def add_event(self, event: TaskEvent) -> None:
        """Add event, creating node if needed. Links child to parent if parent exists."""
        if event.task_id not in self.nodes:
            self.nodes[event.task_id] = TaskNode(
                task_id=event.task_id,
                name=event.name,
                state=event.state,
                parent_id=event.parent_id,
            )
            if event.parent_id is None:
                self.root_ids.append(event.task_id)
            elif event.parent_id in self.nodes:
                self.nodes[event.parent_id].children.append(event.task_id)

        node = self.nodes[event.task_id]
        node.events.append(event)
        node.state = event.state

        # Update parent_id if we didn't have it before (PENDING lacks parent_id)
        if node.parent_id is None and event.parent_id is not None:
            node.parent_id = event.parent_id
            # Remove from root_ids since it has a parent now
            if event.task_id in self.root_ids:
                self.root_ids.remove(event.task_id)
            # Link to parent if parent exists
            if event.parent_id in self.nodes:
                parent = self.nodes[event.parent_id]
                if event.task_id not in parent.children:
                    parent.children.append(event.task_id)

    def get_node(self, task_id: str) -> TaskNode | None:
        """Get node by ID, or None if not found."""
        return self.nodes.get(task_id)
