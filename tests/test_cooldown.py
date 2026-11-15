from datetime import UTC, datetime, timedelta

from mcpool.sources import eligible

NOW = datetime(2026, 11, 15, tzinfo=UTC)


def days_ago(n):
    return NOW - timedelta(days=n)


PUBLISHED = {"1.9.0": days_ago(40), "1.10.0": days_ago(10), "1.11.0": days_ago(2), "1.12.0-rc.1": days_ago(9)}


def test_newest_release_outside_the_cooldown_wins():
    assert eligible(PUBLISHED, "1.9.0", 7, NOW) == "1.10.0"


def test_releases_inside_the_cooldown_are_skipped():
    assert eligible({"1.0.0": days_ago(30), "1.1.0": days_ago(1)}, None, 7, NOW) == "1.0.0"


def test_never_downgrades():
    assert eligible(PUBLISHED, "1.11.0", 7, NOW) == "1.11.0"


def test_pre_releases_are_ignored():
    assert eligible({"1.0.0": days_ago(30), "2.0.0-beta": days_ago(20)}, "1.0.0", 7, NOW) == "1.0.0"


def test_numeric_ordering_not_lexical():
    assert eligible({"1.9.0": days_ago(30), "1.10.0": days_ago(20)}, None, 7, NOW) == "1.10.0"


def test_nothing_old_enough():
    assert eligible({"0.1.0": days_ago(1)}, None, 7, NOW) is None
