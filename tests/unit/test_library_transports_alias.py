"""The deprecated ``stemtrace.library.transports`` alias of ``stemtrace.transports``."""

import importlib
import sys
import warnings

import pytest

from stemtrace.transports import breaker, get_transport, memory, rabbitmq, redis

OLD_PACKAGE = "stemtrace.library.transports"
OLD_SUBMODULES = ("redis", "rabbitmq", "memory", "breaker")
MESSAGE = (
    r"stemtrace\.library\.transports is deprecated and will be removed in "
    r"stemtrace 1\.0; import from stemtrace\.transports instead"
)


@pytest.fixture
def fresh_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget any earlier import of the alias so importing it runs it again."""
    for name in (OLD_PACKAGE, *(f"{OLD_PACKAGE}.{sub}" for sub in OLD_SUBMODULES)):
        monkeypatch.delitem(sys.modules, name, raising=False)


@pytest.mark.usefixtures("fresh_alias")
class TestLibraryTransportsAlias:
    def test_import_package_warns_deprecation(self) -> None:
        with pytest.warns(DeprecationWarning, match=MESSAGE):
            old = importlib.import_module(OLD_PACKAGE)

        assert old.get_transport is get_transport

    @pytest.mark.parametrize("submodule", OLD_SUBMODULES)
    def test_import_submodule_warns_once(self, submodule: str) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            importlib.import_module(f"{OLD_PACKAGE}.{submodule}")

        deprecations = [w for w in caught if w.category is DeprecationWarning]
        assert len(deprecations) == 1
        assert str(deprecations[0].message).startswith(
            "stemtrace.library.transports is deprecated"
        )

    def test_warning_points_at_the_importing_module(self) -> None:
        # __import__ is a builtin, so the first Python frame above the alias
        # (after importlib's own frames, which warnings skips) is this test.
        with pytest.warns(DeprecationWarning, match=MESSAGE) as record:
            __import__(f"{OLD_PACKAGE}.redis")

        assert record[0].filename == __file__

    def test_submodules_reexport_the_same_objects(self) -> None:
        with pytest.warns(DeprecationWarning, match=MESSAGE):
            old_redis = importlib.import_module(f"{OLD_PACKAGE}.redis")
        old_rabbitmq = importlib.import_module(f"{OLD_PACKAGE}.rabbitmq")
        old_memory = importlib.import_module(f"{OLD_PACKAGE}.memory")
        old_breaker = importlib.import_module(f"{OLD_PACKAGE}.breaker")

        assert old_redis.RedisTransport is redis.RedisTransport
        assert old_redis.effective_socket_timeout is redis.effective_socket_timeout
        assert old_redis.XREAD_BLOCK_MS == redis.XREAD_BLOCK_MS
        assert old_rabbitmq.RabbitMQTransport is rabbitmq.RabbitMQTransport
        assert old_rabbitmq.CONNECT_TIMEOUT == rabbitmq.CONNECT_TIMEOUT
        # Deprecated and unused, but exported since 0.3.8: imports keep working.
        assert old_rabbitmq.PUBLISH_SLOW_ATTEMPT == rabbitmq.PUBLISH_SLOW_ATTEMPT == 0.5
        assert "PUBLISH_SLOW_ATTEMPT" in old_rabbitmq.__all__
        assert old_memory.MemoryTransport is memory.MemoryTransport
        assert old_breaker.PublishBreaker is breaker.PublishBreaker

    @pytest.mark.parametrize("submodule", OLD_SUBMODULES)
    def test_alias_all_names_exist_in_new_module(self, submodule: str) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            old = importlib.import_module(f"{OLD_PACKAGE}.{submodule}")
        new = importlib.import_module(f"stemtrace.transports.{submodule}")

        for name in old.__all__:
            assert getattr(old, name) is getattr(new, name), name
