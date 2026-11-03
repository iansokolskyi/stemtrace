"""``similar_name`` for unregistered names (``core/anomalies.py``, D5).

Each case registers some live names, leaves one PENDING task of the unknown
name waiting past the grace and reads the hint off the per-name item.
"""

from difflib import SequenceMatcher

import pytest

from stemtrace.core.anomalies import (
    SIMILAR_NAME_MIN_SCORE,
    AnomalyKind,
    RegistryFacts,
    _live_by_module,
    _similar_name,
)
from tests.unit.anomaly_pipeline import at
from tests.unit.test_anomalies_unregistered import (
    FRESH,
    OLD,
    pending,
    registry,
    run,
    unregistered,
)


def hint(unknown: str, live: tuple[str, ...]) -> str | None:
    """The ``similar_name`` of the unregistered item for ``unknown``."""
    items, _, _ = run([pending("t1", OLD, unknown)], registry(live))
    (item,) = unregistered(items)
    assert item.name == unknown
    return item.similar_name


def ratio(a: str, b: str) -> float:
    """The score the rule uses."""
    return SequenceMatcher(None, a, b).ratio()


def test_minimum_score_is_the_spec_constant() -> None:
    assert SIMILAR_NAME_MIN_SCORE == 0.75


@pytest.mark.parametrize(
    ("unknown", "live", "expected"),
    [
        # A version suffix is stripped on either side.
        ("app.sync_invoices_v2", ("app.sync_invoices",), "app.sync_invoices"),
        ("app.sync_invoices", ("app.sync_invoices_v2",), "app.sync_invoices_v2"),
        ("app.sync_invoices_v2", ("app.sync_invoices_v10",), "app.sync_invoices_v10"),
        # Equal once stripped (score 1.0): the other version is the hint.
        ("app.sync_v2", ("app.sync_v1",), "app.sync_v1"),
        # Short names score below 0.75 until the suffix is stripped.
        ("app.run_v2", ("app.run",), "app.run"),
        ("app.run", ("app.run_v10",), "app.run_v10"),
        # A rename that keeps most of the name.
        ("app.send_invoice", ("app.send_invoices",), "app.send_invoices"),
        # Accepted false positive: a different task with a near name still gets a hint.
        ("app.send_email", ("app.send_emails",), "app.send_emails"),
        # The best of several candidates wins when it is unique.
        (
            "app.sync_invoices_v2",
            ("app.sync_invoices", "app.sync_orders", "app.cleanup"),
            "app.sync_invoices",
        ),
        (
            "app.send_email",
            ("app.send_emails", "app.send_email_now"),
            "app.send_emails",
        ),
        # Nothing close enough.
        ("app.sync_invoices", ("app.send_email",), None),
        # No live names at all, so no candidates.
        ("app.sync_invoices", (), None),
        # The same last segment in another module is not a candidate.
        ("app.sync_invoices_v2", ("other.sync_invoices",), None),
        ("app.sub.sync_invoices_v2", ("app.sync_invoices",), None),
        # Built-ins are not candidates.
        ("app.sync_invoices_v2", ("celery.sync_invoices",), None),
        # A tie gives no hint, even for a perfect match.
        ("app.sync_v3", ("app.sync_v1", "app.sync_v2"), None),
        ("app.send_emaiq", ("app.send_emails", "app.send_emailz"), None),
        # Dotless names have no module: neither side gets a hint.
        ("sync_invoices_v2", ("sync_invoices",), None),
        ("app.sync_invoices_v2", ("sync_invoices",), None),
        ("sync_invoices", ("app.sync_invoices",), None),
        # A segment that is only a version has nothing left to compare.
        ("app._v2", ("app._v1",), None),
        ("app.", ("app.x",), None),
        # Case matters: Celery names are case-sensitive.
        ("app.SEND_EMAIL", ("app.send_email",), None),
        ("App.sync_invoices_v2", ("app.sync_invoices",), None),
        # A suffix that is not _v<digits> at the end is kept.
        ("app.sync_v2x", ("app.sync",), None),
        ("app.v2_sync", ("app.sync",), None),
    ],
)
def test_similar_name_table(
    unknown: str, live: tuple[str, ...], expected: str | None
) -> None:
    assert hint(unknown, live) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected_ratio", "hinted"),
    [
        ("abcdefghijklmn", "abcdefghijkxxxx", 0.7586206896551724, True),
        ("abcdefgh", "abcdefxx", 0.75, True),  # the threshold is inclusive
        ("a" * 13 + "b" * 4, "a" * 13 + "c" * 5, 0.7428571428571429, False),
    ],
)
def test_score_just_above_at_and_just_below_the_threshold(
    a: str, b: str, expected_ratio: float, hinted: bool
) -> None:
    assert ratio(a, b) == pytest.approx(expected_ratio)

    assert hint(f"app.{a}", (f"app.{b}",)) == (f"app.{b}" if hinted else None)


def test_hint_is_on_the_per_name_item_and_its_task_node() -> None:
    items, nodes, _ = run(
        [pending("t1", OLD, "app.sync_invoices_v2")], registry(("app.sync_invoices",))
    )

    assert [a.kind for a in items] == [AnomalyKind.UNREGISTERED]
    assert items[0].similar_name == "app.sync_invoices"
    assert nodes["t1"].similar_name == "app.sync_invoices"


def test_hint_candidates_are_the_registry_live_names() -> None:
    facts = RegistryFacts(
        live_names=frozenset({"app.other"}),
        any_live=True,
        authority_at=at(FRESH),
        broker_configured=True,
    )

    items, _, _ = run([pending("t1", OLD, "app.sync_invoices_v2")], facts)

    assert unregistered(items)[0].similar_name is None


def test_builtin_names_are_never_candidates() -> None:
    # Not reachable through classify_anomalies (an unregistered name is never
    # celery.*, so no candidate shares its module); this guards the helper.
    live = _live_by_module({"celery.sync_invoices", "app.other"})

    assert _similar_name("celery.sync_invoices_v2", live) is None
