"""Task groups: which named group (and tags) a task belongs to.

A task group is a label for related tasks (``billing``, ``search``), not Celery's
``group()`` canvas primitive. Every task has exactly one task group:

1. The first code-defined :class:`GroupRule` whose patterns match the task, in
   the order the rules were given.
2. Otherwise its automatic group, derived from the task name by
   :func:`automatic_group`.

Pure logic: no I/O. Loading rules from a file is the server's job.
"""

from collections.abc import Iterable, Sequence
from enum import Enum
from fnmatch import fnmatchcase
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

UNGROUPED = "ungrouped"

# Module names that say where code lives, not what it does (``app.tasks``,
# ``src.workers``). Compared case-insensitively.
GENERIC_SEGMENTS: frozenset[str] = frozenset(
    {"tasks", "app", "apps", "celery", "jobs", "worker", "workers", "src"}
)

_NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_HexColor = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"
    ),
]


class GroupSource(str, Enum):
    """Where a task group comes from."""

    AUTO = "auto"  # derived from task names
    CODE = "code"  # defined by a GroupRule


class TaskAttributes(BaseModel):
    """What a group rule can match on.

    A model rather than a bare name so rules can match more attributes (the
    queue, once events carry it) without changing every caller.
    """

    model_config = ConfigDict(frozen=True)

    name: str


class GroupRule(BaseModel):
    """A code-defined task group.

    Example:
        GroupRule(name="billing", match=["billing.*", "*.charge_*"], tags=["money"])

    Attributes:
        name: Group name shown in the UI and API.
        match: Glob patterns on the full task name (``fnmatch`` syntax,
            case-sensitive; ``*`` also matches dots). A task matches if any
            pattern does.
        tags: Tags given to every task in the group.
        color: Optional ``#rgb`` or ``#rrggbb`` color for the UI.
    """

    # extra="forbid": a typo in a config file (``matches``, ``tag``) is an
    # error instead of a rule that silently never applies.
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Sequence so callers can pass lists; stored as tuples (hashable, frozen).
    name: _NonEmptyStr
    match: Sequence[_NonEmptyStr] = Field(min_length=1)
    tags: Sequence[_NonEmptyStr] = ()
    color: _HexColor | None = None

    @field_validator("match")
    @classmethod
    def _as_tuple(cls, match: Sequence[str]) -> tuple[str, ...]:
        return tuple(match)

    @field_validator("tags")
    @classmethod
    def _dedupe_tags(cls, tags: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(tags))

    def matches(self, task: TaskAttributes) -> bool:
        """Whether any of this rule's patterns matches the task's name."""
        return any(fnmatchcase(task.name, pattern) for pattern in self.match)


class ResolvedGroup(BaseModel):
    """The task group a task belongs to."""

    model_config = ConfigDict(frozen=True)

    name: str
    source: GroupSource
    tags: tuple[str, ...] = ()
    color: str | None = None


def automatic_group(task_name: str) -> str:
    """Derive a task's group from its dotted name.

    Only module segments (all but the last, which is the task function) can
    name a group; otherwise every task would be its own group. Leading generic
    module segments (:data:`GENERIC_SEGMENTS`) are skipped, so
    ``tasks.search.reindex`` and ``search.tasks.reindex`` both give ``search``.
    Empty segments and surrounding whitespace are ignored.

    Args:
        task_name: Full Celery task name.

    Returns:
        The first non-generic module segment, or :data:`UNGROUPED` if there is
        none (``add``, ``tasks.add``, ``app.tasks.add``, ``celery.chord_unlock``).
    """
    segments = [s.strip() for s in task_name.split(".")]
    modules = [s for s in segments if s][:-1]
    return next(
        (s for s in modules if s.casefold() not in GENERIC_SEGMENTS),
        UNGROUPED,
    )


class GroupConfig(BaseModel):
    """Code-defined group rules plus the automatic fallback.

    ``GroupConfig()`` (no rules) gives every task its automatic group.

    Raises:
        pydantic.ValidationError: On an invalid rule or two rules with the
            same name.
    """

    model_config = ConfigDict(frozen=True)

    rules: Sequence[GroupRule] = ()

    @field_validator("rules")
    @classmethod
    def _rules_as_tuple(cls, rules: Sequence[GroupRule]) -> tuple[GroupRule, ...]:
        return tuple(rules)

    @model_validator(mode="after")
    def _unique_names(self) -> "GroupConfig":
        first_index: dict[str, int] = {}
        for index, rule in enumerate(self.rules):
            if rule.name in first_index:
                raise ValueError(
                    f"duplicate group name {rule.name!r} "
                    f"(entries {first_index[rule.name]} and {index})"
                )
            first_index[rule.name] = index
        return self

    def resolve(self, task: TaskAttributes) -> ResolvedGroup:
        """Return the task's group: the first matching rule, else its automatic group.

        A task whose automatic group has the same name as a rule joins that
        rule's group (with its tags and color), so each name is one group.
        """
        rule = next((r for r in self.rules if r.matches(task)), None)
        if rule is None:
            auto_name = automatic_group(task.name)
            rule = next((r for r in self.rules if r.name == auto_name), None)
            if rule is None:
                return ResolvedGroup(name=auto_name, source=GroupSource.AUTO)
        return _code_group(rule)

    def members(self, task_names: Iterable[str]) -> list["GroupMembers"]:
        """Bucket task names by group.

        Every code-defined group is listed, with no tasks if none matched yet,
        so a configured group is visible before its tasks run.

        Args:
            task_names: Task names to place (duplicates are ignored).

        Returns:
            Groups sorted by name with :data:`UNGROUPED` last, each with its
            task names sorted.
        """
        buckets: dict[str, tuple[ResolvedGroup, set[str]]] = {
            rule.name: (_code_group(rule), set()) for rule in self.rules
        }
        for name in task_names:
            group = self.resolve(TaskAttributes(name=name))
            buckets.setdefault(group.name, (group, set()))[1].add(name)
        return [
            GroupMembers(group=group, task_names=tuple(sorted(names)))
            for group, names in sorted(
                buckets.values(),
                key=lambda item: (item[0].name == UNGROUPED, item[0].name),
            )
        ]


class GroupMembers(BaseModel):
    """A task group and the task names in it."""

    model_config = ConfigDict(frozen=True)

    group: ResolvedGroup
    task_names: tuple[str, ...]


def _code_group(rule: GroupRule) -> ResolvedGroup:
    return ResolvedGroup(
        name=rule.name,
        source=GroupSource.CODE,
        tags=tuple(rule.tags),
        color=rule.color,
    )
