"""Build and validate the anomaly settings from arguments and env vars."""

import math
import os
from datetime import timedelta

from stemtrace.core.anomalies import (
    DEFAULT_SETTINGS,
    MAX_STUCK_FACTOR,
    AnomalySettings,
)
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server.stats import MAX_DURATION, parse_duration

__all__ = [
    "CEILING_ENV_VAR",
    "FACTOR_ENV_VAR",
    "FLOOR_ENV_VAR",
    "GRACE_ENV_VAR",
    "MIN_SAMPLES_ENV_VAR",
    "WINDOW_ENV_VAR",
    "anomaly_settings",
    "configured_anomaly_settings",
]

FACTOR_ENV_VAR = "STEMTRACE_ANOMALY_STUCK_FACTOR"
FLOOR_ENV_VAR = "STEMTRACE_ANOMALY_STUCK_FLOOR"
CEILING_ENV_VAR = "STEMTRACE_ANOMALY_STUCK_CEILING"
MIN_SAMPLES_ENV_VAR = "STEMTRACE_ANOMALY_STUCK_MIN_SAMPLES"
GRACE_ENV_VAR = "STEMTRACE_ANOMALY_PENDING_GRACE"
WINDOW_ENV_VAR = "STEMTRACE_ANOMALY_REGISTRY_WINDOW"

_PARAMETER_LABELS = (
    "anomaly_stuck_factor",
    "anomaly_stuck_floor",
    "anomaly_stuck_ceiling",
    "anomaly_stuck_min_samples",
    "anomaly_pending_grace",
    "anomaly_registry_window",
)
_DURATION_HELP = (
    f"a positive duration like 90s, 10m, 1h or 7d (at most {MAX_DURATION.days}d)"
)


def anomaly_settings(
    *,
    stuck_factor: float | str | None = None,
    stuck_floor: timedelta | str | None = None,
    stuck_ceiling: timedelta | str | None = None,
    stuck_min_samples: int | str | None = None,
    pending_grace: timedelta | str | None = None,
    authority_window: timedelta | str | None = None,
) -> AnomalySettings:
    """Validate the anomaly settings; None keeps the default. Reads no env vars.

    Durations are a ``timedelta`` or a string like ``90s``, ``10m``, ``1h``
    or ``7d``.

    Raises:
        ConfigurationError: If a value isn't positive (``stuck_min_samples``
            at least 1, ``stuck_factor`` at most ``MAX_STUCK_FACTOR``).
    """
    return _build(
        factor=_Input(stuck_factor, _PARAMETER_LABELS[0]),
        floor=_Input(stuck_floor, _PARAMETER_LABELS[1]),
        ceiling=_Input(stuck_ceiling, _PARAMETER_LABELS[2]),
        min_samples=_Input(stuck_min_samples, _PARAMETER_LABELS[3]),
        grace=_Input(pending_grace, _PARAMETER_LABELS[4]),
        window=_Input(authority_window, _PARAMETER_LABELS[5]),
    )


def configured_anomaly_settings(
    *,
    stuck_factor: float | str | None = None,
    stuck_floor: timedelta | str | None = None,
    stuck_ceiling: timedelta | str | None = None,
    stuck_min_samples: int | str | None = None,
    pending_grace: timedelta | str | None = None,
    authority_window: timedelta | str | None = None,
    argument_labels: tuple[str, str, str, str, str, str] | None = None,
) -> AnomalySettings:
    """As ``anomaly_settings``, but a missing or blank argument reads its env var.

    The variables are ``STEMTRACE_ANOMALY_STUCK_*`` (``FACTOR``, ``FLOOR``,
    ``CEILING``, ``MIN_SAMPLES``), ``STEMTRACE_ANOMALY_PENDING_GRACE`` and
    ``STEMTRACE_ANOMALY_REGISTRY_WINDOW``; a blank one counts as unset.
    ``argument_labels`` renames the arguments in error messages (the CLI flags).

    Raises:
        ConfigurationError: As ``anomaly_settings``; the message names the
            env var when the value came from one.
    """
    factor, floor, ceiling, samples, grace, window = (
        argument_labels or _PARAMETER_LABELS
    )
    return _build(
        factor=_from_env(stuck_factor, factor, FACTOR_ENV_VAR),
        floor=_from_env(stuck_floor, floor, FLOOR_ENV_VAR),
        ceiling=_from_env(stuck_ceiling, ceiling, CEILING_ENV_VAR),
        min_samples=_from_env(stuck_min_samples, samples, MIN_SAMPLES_ENV_VAR),
        grace=_from_env(pending_grace, grace, GRACE_ENV_VAR),
        window=_from_env(authority_window, window, WINDOW_ENV_VAR),
    )


class _Input:
    """A raw setting and the name an error message calls it."""

    def __init__(self, value: object, label: str) -> None:
        """Keep the raw value and its label."""
        self.value = value
        self.label = label


def _from_env(argument: object, parameter: str, env_var: str) -> _Input:
    if not _is_blank(argument):
        return _Input(argument, parameter)
    from_env = os.getenv(env_var)
    return _Input(None if _is_blank(from_env) else from_env, env_var)


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _build(
    *,
    factor: _Input,
    floor: _Input,
    ceiling: _Input,
    min_samples: _Input,
    grace: _Input,
    window: _Input,
) -> AnomalySettings:
    defaults = DEFAULT_SETTINGS
    return AnomalySettings(
        stuck_factor=defaults.stuck_factor if factor.value is None else _factor(factor),
        stuck_floor=defaults.stuck_floor if floor.value is None else _duration(floor),
        stuck_ceiling=defaults.stuck_ceiling
        if ceiling.value is None
        else _duration(ceiling),
        stuck_min_samples=defaults.stuck_min_samples
        if min_samples.value is None
        else _min_samples(min_samples),
        pending_grace=defaults.pending_grace
        if grace.value is None
        else _duration(grace),
        authority_window=defaults.authority_window
        if window.value is None
        else _duration(window),
    )


def _factor(setting: _Input) -> float:
    value = setting.value
    error = ConfigurationError(
        f"{setting.label} must be a positive number of at most {MAX_STUCK_FACTOR}, "
        f"got {value!r}."
    )
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise error
    try:
        number = float(value)
    except ValueError:
        raise error from None
    if not math.isfinite(number) or not 0 < number <= MAX_STUCK_FACTOR:
        raise error
    return number


def _duration(setting: _Input) -> timedelta:
    value = setting.value
    if isinstance(value, timedelta):
        if timedelta(0) < value <= MAX_DURATION:
            return value
    elif isinstance(value, str):
        try:
            return parse_duration(value.strip())
        except ValueError:
            pass
    raise ConfigurationError(
        f"{setting.label} must be {_DURATION_HELP}, got {value!r}."
    )


def _min_samples(setting: _Input) -> int:
    value = setting.value
    error = ConfigurationError(
        f"{setting.label} must be a whole number of at least 1, got {value!r}."
    )
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise error
    try:
        number = int(value)
    except ValueError:
        raise error from None
    if number < 1:
        raise error
    return number
