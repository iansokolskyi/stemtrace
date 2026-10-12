"""The publisher against a real RabbitMQ behind a misbehaving TCP proxy.

Skipped when RabbitMQ (with the management API) isn't reachable. The proxy
delays what the broker says, or stops in the middle of a frame, which is how a
slow or frozen broker looks to a client: the broker itself stays healthy.
"""

import contextlib
import os
import socket
import threading
import time
import urllib.parse
from collections.abc import Iterator

import pytest
from kombu import Connection

from stemtrace.transports.rabbitmq import RabbitMQTransport
from tests.integration.test_rabbitmq_publisher_broker import (
    RABBITMQ_URL,
    _available,
    _current_name,
    _event,
    _management,
    _received,
    _wait_for,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _available(), reason="RabbitMQ with management API not available"
    ),
]


class _Proxy:
    """TCP proxy to the broker that can delay or cut off what the broker sends."""

    def __init__(self, upstream: tuple[str, int]) -> None:
        self._upstream = upstream
        # Seconds each chunk from the broker is held back.
        self.delay = 0.0
        # While set, only the first bytes of the next chunk from the broker go
        # through (a frame cut in half), and the rest waits for release().
        self.cut_next_chunk_to: int | None = None
        self._release = threading.Event()
        self._closing = threading.Event()
        self._sockets: list[socket.socket] = []
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._server.settimeout(0.2)
        self.port: int = self._server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def release(self) -> None:
        """Let a held back (cut) chunk through."""
        self._release.set()

    def close(self) -> None:
        self._closing.set()
        self._release.set()
        for sock in self._sockets:
            with contextlib.suppress(OSError):
                sock.close()
        self._server.close()

    def _accept(self) -> None:
        while not self._closing.is_set():
            try:
                client, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            upstream = socket.create_connection(self._upstream, timeout=5)
            upstream.settimeout(None)
            self._sockets += [client, upstream]
            threading.Thread(
                target=self._pump, args=(client, upstream, False), daemon=True
            ).start()
            threading.Thread(
                target=self._pump, args=(upstream, client, True), daemon=True
            ).start()

    def _pump(
        self, source: socket.socket, sink: socket.socket, from_broker: bool
    ) -> None:
        try:
            while not self._closing.is_set():
                data = source.recv(65536)
                if not data:
                    break
                if from_broker:
                    if self.delay:
                        time.sleep(self.delay)
                    cut = self.cut_next_chunk_to
                    if cut is not None:
                        self.cut_next_chunk_to = None
                        sink.sendall(data[:cut])
                        self._release.wait()
                        data = data[cut:]
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)


@pytest.fixture
def proxy() -> Iterator[_Proxy]:
    parsed = Connection(RABBITMQ_URL)
    broker = _Proxy((parsed.hostname or "localhost", int(parsed.port or 5672)))
    yield broker
    broker.close()


@pytest.fixture
def transport(proxy: _Proxy) -> Iterator[RabbitMQTransport]:
    prefix = f"proxy_{os.getpid()}_{int(time.time() * 1000)}"
    direct = RabbitMQTransport(RABBITMQ_URL, prefix=prefix, ttl=60)
    direct._declare_exchange_and_queue()
    parts = urllib.parse.urlsplit(RABBITMQ_URL)
    via_proxy = parts._replace(
        netloc=f"{parts.netloc.rsplit('@', 1)[0]}@127.0.0.1:{proxy.port}"
    )
    publisher = RabbitMQTransport(via_proxy.geturl(), prefix=prefix, ttl=60)
    yield publisher
    publisher.close()
    with contextlib.suppress(Exception), Connection(RABBITMQ_URL) as conn:
        channel = conn.channel()
        channel.queue_delete(direct.queue_name)
        channel.exchange_delete(direct.exchange_name)


class TestSlowBroker:
    def test_publish_from_threads_while_broker_answers_slowly_keeps_breaker_closed(
        self, proxy: _Proxy, transport: RabbitMQTransport
    ) -> None:
        # Each chunk the broker sends takes a second: connecting (Start, Tune,
        # Open-Ok, channel, declare) takes several seconds with the connection
        # lock held, every step within its own 2s timeout.
        proxy.delay = 1.0
        threads = [
            threading.Thread(target=transport.publish, args=(_event(f"slow-{i}"),))
            for i in range(4)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert [t.is_alive() for t in threads] == [False] * 4
        assert transport._publish_breaker.is_open is False
        assert sorted(_received(transport.queue_name, 4)) == [
            f"slow-{i}" for i in range(4)
        ]


class TestFrozenBrokerMidFrame:
    def test_publish_while_close_frame_is_half_received_returns_without_spinning(
        self, proxy: _Proxy, transport: RabbitMQTransport
    ) -> None:
        transport.publish(_event("before"))
        name = _current_name(os.getpid())
        # The broker sends Connection.Close; the proxy passes its first 7
        # bytes (the frame header) and freezes.
        proxy.cut_next_chunk_to = 7
        _management("DELETE", f"connections/{urllib.parse.quote(name, safe='')}")
        _wait_for(lambda: proxy.cut_next_chunk_to is None, "the close frame")
        time.sleep(0.2)
        finished: list[float] = []

        def publish() -> None:
            cpu = time.process_time()
            transport.publish(_event("during"))
            finished.append(time.process_time() - cpu)

        thread = threading.Thread(target=publish, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(timeout=5)
        returned_in = time.monotonic() - started
        stuck = thread.is_alive()
        if stuck:
            import sys
            import traceback

            print("".join(traceback.format_stack(sys._current_frames()[thread.ident])))
        proxy.release()  # the rest of the frame arrives (ends a spinning read too)
        thread.join(timeout=10)

        assert stuck is False
        assert returned_in < 1.0
        assert finished[0] < 0.5  # a spinning read burns a CPU second per second
        # Once the frame is complete the connection is replaced and publishing
        # goes on ("during" went to a connection the broker was closing).
        time.sleep(0.3)
        transport.publish(_event("after-1"))
        transport.publish(_event("after-2"))
        assert _received(transport.queue_name, 3) == ["before", "after-1", "after-2"]
