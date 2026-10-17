"""Domain exceptions for stemtrace."""


class StemtraceError(Exception):
    """Base exception for stemtrace."""


class ConfigurationError(StemtraceError):
    """Invalid or missing configuration."""


class TransportError(StemtraceError):
    """Transport connection, publish, or consume failure."""


class UnsupportedBrokerError(ConfigurationError):
    """Broker URL scheme not supported."""

    def __init__(self, scheme: str) -> None:
        """Initialize with the unsupported broker scheme."""
        self.scheme = scheme
        super().__init__(
            f"Unsupported broker scheme: '{scheme}'. "
            f"Supported: redis, rediss, amqp, amqps, pyamqp, memory"
        )


# Celery's time-limit and termination exceptions are billiard's classes
# (celery.exceptions re-exports them, so ``__module__`` stays billiard's).
# They are matched by module and name because core/ and transports/ must not
# import celery or billiard. billiard 4.2.4: ``SoftTimeLimitExceeded`` is
# raised into the task's own thread by a SIGUSR1 handler
# (``pool.soft_timeout_sighandler``, installed for SIGUSR1 in the child), so
# it can land on any line, including one inside stemtrace's publishing and
# capture code. The other three are raised in the pool's parent process, not
# in task code (``TimeLimitExceeded`` in ``Pool.on_hard_timeout``,
# ``Terminated`` in ``ApplyResult._set_terminated``, ``WorkerLostError`` when a
# child dies); they are listed so that no billiard control-flow exception is
# ever mistaken for a broker fault. gevent's and eventlet's ``Timeout`` derive
# from BaseException, which ``except Exception`` never catches.
_INTERRUPT_MODULE = "billiard.exceptions"
_INTERRUPT_NAMES = frozenset(
    {"SoftTimeLimitExceeded", "TimeLimitExceeded", "Terminated", "WorkerLostError"}
)


def is_task_interrupt(exc: BaseException) -> bool:
    """Whether ``exc`` is Celery control flow that must never be swallowed.

    A soft time limit is delivered as an exception raised asynchronously into
    the task's thread, so it can surface inside any ``except Exception`` that
    wraps a publish or a capture hook. Such a handler must re-raise it instead
    of treating it as a failure of its own work (a broker fault, say).

    Args:
        exc: The exception caught.

    Returns:
        True for Celery's ``SoftTimeLimitExceeded``, ``TimeLimitExceeded``,
        ``Terminated`` and ``WorkerLostError``, and their subclasses.
    """
    return any(
        klass.__module__ == _INTERRUPT_MODULE and klass.__name__ in _INTERRUPT_NAMES
        for klass in type(exc).__mro__
    )
