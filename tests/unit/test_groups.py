"""Tests for task group rules (core/groups.py)."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from stemtrace.core.groups import (
    GENERIC_SEGMENTS,
    UNGROUPED,
    GroupConfig,
    GroupRule,
    GroupSource,
    ResolvedGroup,
    TaskAttributes,
    automatic_group,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _task(name: str) -> TaskAttributes:
    return TaskAttributes(name=name)


@pytest.mark.parametrize(
    ("task_name", "expected"),
    [
        # flat: no module, nothing to group by
        ("add", UNGROUPED),
        ("reindex_everything", UNGROUPED),
        # dotted
        ("search.reindex", "search"),
        ("billing.charge", "billing"),
        # module path
        ("search.tasks.reindex", "search"),
        ("billing.tasks.charge", "billing"),
        # generic prefix: skipped, the next module segment names the group
        ("tasks.search.reindex", "search"),
        ("app.billing.tasks.charge", "billing"),
        ("apps.billing.charge", "billing"),
        ("src.app.billing.charge", "billing"),
        ("jobs.export.run", "export"),
        ("worker.mail.send", "mail"),
        ("workers.mail.send", "mail"),
        # generic segments compare case-insensitively; the group keeps its case
        ("Tasks.Search.reindex", "Search"),
        ("APP.billing.charge", "billing"),
        # only generic modules: no meaningful group
        ("tasks.add", UNGROUPED),
        ("app.tasks.add", UNGROUPED),
        ("src.app.tasks.workers.add", UNGROUPED),
        ("celery.chord_unlock", UNGROUPED),
        ("celery.accumulate", UNGROUPED),
        # a generic word as the task function itself is not a module
        ("search.tasks", "search"),
        ("search.celery", "search"),
        # deep modules: the first meaningful one wins
        ("a.b.c.d.e.f", "a"),
        ("company.platform.search.tasks.reindex", "company"),
        # unicode passes through unchanged
        ("données.tâches.exporter", "données"),
        ("搜索.tasks.reindex", "搜索"),
        ("tasks.ödeme.al", "ödeme"),
        # empty segments and whitespace are ignored
        ("", UNGROUPED),
        ("   ", UNGROUPED),
        (".", UNGROUPED),
        ("...", UNGROUPED),
        (".add", UNGROUPED),
        ("add.", UNGROUPED),
        ("search..reindex", "search"),
        ("..search.reindex", "search"),
        ("search.reindex.", "search"),
        ("tasks..search.reindex", "search"),
        ("  search.reindex  ", "search"),
        (" search . reindex", "search"),
    ],
)
def test_automatic_group_by_name_shape_returns_expected_group(
    task_name: str, expected: str
) -> None:
    assert automatic_group(task_name) == expected


def test_generic_segments_are_the_documented_list() -> None:
    assert (
        frozenset(
            {"tasks", "app", "apps", "celery", "jobs", "worker", "workers", "src"}
        )
        == GENERIC_SEGMENTS
    )


@pytest.mark.parametrize("generic", sorted(GENERIC_SEGMENTS))
def test_automatic_group_with_each_generic_prefix_uses_next_segment(
    generic: str,
) -> None:
    assert automatic_group(f"{generic}.search.reindex") == "search"


def _real_task_names() -> set[str]:
    """Task names from recorded Celery flows and the bundled examples."""
    flows = (_REPO_ROOT / "tests" / "data" / "celery_flows.json").read_text()
    names = set(re.findall(r'"name": "([^"]+)"', flows))
    examples = (_REPO_ROOT / "examples" / "celery_app.py").read_text()
    names |= set(re.findall(r'name="([a-z_.]+)"', examples))
    return names


def test_automatic_group_on_real_task_names_groups_by_app_module() -> None:
    names = _real_task_names()
    # Fail, don't skip, if the recorded data moved.
    assert "flows.add" in names
    assert "examples.celery_app.add" in names

    groups = {name: automatic_group(name) for name in names}

    assert {g for n, g in groups.items() if n.startswith("flows.")} == {"flows"}
    assert {g for n, g in groups.items() if n.startswith("examples.")} == {"examples"}
    assert groups["celery.chord_unlock"] == UNGROUPED
    assert groups["celery.accumulate"] == UNGROUPED


# --- GroupRule -------------------------------------------------------------


def test_group_rule_matches_any_of_its_globs() -> None:
    rule = GroupRule(name="money", match=["billing.*", "*.charge_*"])

    assert rule.matches(_task("billing.tasks.refund")) is True
    assert rule.matches(_task("shop.charge_card")) is True
    assert rule.matches(_task("shop.refund")) is False


def test_group_rule_glob_star_crosses_dots() -> None:
    rule = GroupRule(name="search", match=["search.*"])

    assert rule.matches(_task("search.tasks.deep.reindex")) is True
    assert rule.matches(_task("searchx.reindex")) is False


def test_group_rule_glob_is_case_sensitive() -> None:
    rule = GroupRule(name="search", match=["search.*"])

    assert rule.matches(_task("Search.reindex")) is False


def test_group_rule_supports_character_classes_and_single_char() -> None:
    rule = GroupRule(name="shards", match=["shard[0-9].run", "job?.go"])

    assert rule.matches(_task("shard7.run")) is True
    assert rule.matches(_task("jobs.go")) is True
    assert rule.matches(_task("shardx.run")) is False


def test_group_rule_strips_and_dedupes_tags() -> None:
    rule = GroupRule(
        name=" billing ", match=["billing.*"], tags=["money", " money ", "pci"]
    )

    assert rule.name == "billing"
    assert rule.tags == ("money", "pci")


def test_group_rule_is_frozen() -> None:
    rule = GroupRule(name="billing", match=["billing.*"])

    with pytest.raises(ValidationError):
        rule.name = "other"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "match": ["a.*"]},
        {"name": "   ", "match": ["a.*"]},
        {"name": "a", "match": []},
        {"name": "a", "match": [""]},
        {"name": "a"},
        {"name": "a", "match": "a.*"},  # a bare string is not a list of globs
        {"name": "a", "match": ["a.*"], "tags": [""]},
        {"name": "a", "match": ["a.*"], "color": "red"},
        {"name": "a", "match": ["a.*"], "color": "#12345"},
        {"name": "a", "match": ["a.*"], "color": "#abc; background: url(x)"},
        {"name": "a", "match": ["a.*"], "matches": ["a.*"]},  # typo: unknown key
        {"name": "a", "match": ["a.*"], "queue": ["q"]},  # not supported yet
    ],
)
def test_group_rule_with_invalid_fields_raises(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GroupRule.model_validate(kwargs)


@pytest.mark.parametrize("color", ["#abc", "#ABCDEF", "#09afAF", " #123 "])
def test_group_rule_accepts_hex_colors(color: str) -> None:
    assert GroupRule(name="a", match=["a.*"], color=color).color == color.strip()


# --- GroupConfig.resolve ---------------------------------------------------


def test_resolve_without_rules_returns_automatic_group() -> None:
    resolved = GroupConfig().resolve(_task("search.tasks.reindex"))

    assert resolved == ResolvedGroup(name="search", source=GroupSource.AUTO)


def test_resolve_matching_rule_returns_code_group_with_tags_and_color() -> None:
    config = GroupConfig(
        rules=[
            GroupRule(
                name="money",
                match=["billing.*"],
                tags=["pci", "critical"],
                color="#f00",
            )
        ]
    )

    resolved = config.resolve(_task("billing.tasks.charge"))

    assert resolved == ResolvedGroup(
        name="money", source=GroupSource.CODE, tags=("pci", "critical"), color="#f00"
    )


def test_resolve_with_overlapping_rules_first_rule_wins() -> None:
    config = GroupConfig(
        rules=[
            GroupRule(name="everything", match=["*"], tags=["all"]),
            GroupRule(name="search", match=["search.*"], tags=["search"]),
        ]
    )

    assert config.resolve(_task("search.reindex")).name == "everything"
    assert config.resolve(_task("search.reindex")).tags == ("all",)


def test_resolve_with_overlapping_rules_in_other_order_specific_wins() -> None:
    config = GroupConfig(
        rules=[
            GroupRule(name="search", match=["search.*"]),
            GroupRule(name="everything", match=["*"]),
        ]
    )

    assert config.resolve(_task("search.reindex")).name == "search"
    assert config.resolve(_task("billing.charge")).name == "everything"


def test_resolve_unmatched_task_falls_back_to_automatic_group() -> None:
    config = GroupConfig(rules=[GroupRule(name="money", match=["billing.*"])])

    resolved = config.resolve(_task("tasks.search.reindex"))

    assert resolved == ResolvedGroup(name="search", source=GroupSource.AUTO)


def test_resolve_automatic_name_equal_to_rule_name_joins_that_group() -> None:
    config = GroupConfig(
        rules=[
            GroupRule(name="search", match=["search_v2.*"], tags=["s"], color="#0f0")
        ]
    )

    resolved = config.resolve(_task("search.tasks.reindex"))

    assert resolved == ResolvedGroup(
        name="search", source=GroupSource.CODE, tags=("s",), color="#0f0"
    )


def test_resolve_rule_named_ungrouped_claims_ungrouped_tasks() -> None:
    config = GroupConfig(
        rules=[GroupRule(name=UNGROUPED, match=["nothing"], tags=["t"])]
    )

    resolved = config.resolve(_task("add"))

    assert resolved.source == GroupSource.CODE
    assert resolved.tags == ("t",)


def test_group_config_with_duplicate_rule_names_raises() -> None:
    with pytest.raises(ValidationError, match="duplicate group name 'billing'"):
        GroupConfig(
            rules=[
                GroupRule(name="billing", match=["billing.*"]),
                GroupRule(name="billing", match=["payments.*"]),
            ]
        )


def test_group_config_validates_rules_given_as_dicts() -> None:
    config = GroupConfig.model_validate(
        {"rules": [{"name": "money", "match": ["billing.*"], "tags": ["pci"]}]}
    )

    assert config.rules == (GroupRule(name="money", match=["billing.*"], tags=["pci"]),)


# --- GroupConfig.members ---------------------------------------------------


def test_members_buckets_names_sorted_with_ungrouped_last() -> None:
    config = GroupConfig(
        rules=[GroupRule(name="money", match=["billing.*"], tags=["pci"], color="#abc")]
    )

    members = config.members(
        ["search.reindex", "add", "billing.charge", "tasks.search.index", "add"]
    )

    assert [(m.group.name, m.group.source, m.task_names) for m in members] == [
        ("money", GroupSource.CODE, ("billing.charge",)),
        ("search", GroupSource.AUTO, ("search.reindex", "tasks.search.index")),
        (UNGROUPED, GroupSource.AUTO, ("add",)),
    ]
    assert members[0].group.tags == ("pci",)
    assert members[0].group.color == "#abc"


def test_members_lists_code_groups_without_tasks() -> None:
    config = GroupConfig(
        rules=[
            GroupRule(name="zeta", match=["zeta.*"]),
            GroupRule(name="alpha", match=["alpha.*"], tags=["a"]),
        ]
    )

    members = config.members([])

    assert [(m.group.name, m.group.source, m.task_names) for m in members] == [
        ("alpha", GroupSource.CODE, ()),
        ("zeta", GroupSource.CODE, ()),
    ]


def test_members_without_rules_or_names_is_empty() -> None:
    assert GroupConfig().members([]) == []
