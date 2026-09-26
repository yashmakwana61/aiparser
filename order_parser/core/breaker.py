from __future__ import annotations

import threading
import time
from typing import Callable

import structlog

from order_parser.core import metrics

logger = structlog.get_logger(__name__)

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised instead of calling a dependency while its circuit is open."""


class CircuitBreaker:
    """Thread-safe closed/open/half-open circuit breaker.

    - ``closed``: requests flow; consecutive failures are counted.
    - ``open``: requests are rejected immediately (fail fast).
    - ``half_open``: after ``recovery_seconds``, exactly one probe request
      is allowed through; its outcome decides between closing and reopening.

    A monotonic clock is injected for tests. State changes emit
    ``circuit_transitions_total{circuit,state}``; rejections emit
    ``circuit_requests_rejected_total{circuit}``.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_seconds = float(recovery_seconds)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state = CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._probe_in_flight = False

    # ---------------------------------------------------------------- state

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _transition_locked(self, new_state: str) -> None:
        if new_state != self._state:
            self._state = new_state
            metrics.incr("circuit_transitions_total", circuit=self.name, state=new_state)
            logger.warning(
                "circuit.transitioned",
                circuit=self.name,
                state=new_state,
                failures=self._consecutive_failures,
            )

    # ------------------------------------------------------------------ api

    def allow(self) -> bool:
        """Whether a request may proceed right now."""
        now = self._clock()
        with self._lock:
            if self._state == CLOSED:
                return True
            if self._state == OPEN:
                if now - self._opened_at < self.recovery_seconds:
                    metrics.incr("circuit_requests_rejected_total", circuit=self.name)
                    return False
                # Cooldown elapsed: become half-open and grant the probe.
                self._transition_locked(HALF_OPEN)
                self._probe_in_flight = True
                return True
            # HALF_OPEN: only the first probe gets through.
            if self._probe_in_flight:
                metrics.incr("circuit_requests_rejected_total", circuit=self.name)
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._probe_in_flight = False
            self._transition_locked(CLOSED)

    def record_failure(self) -> None:
        now = self._clock()
        with self._lock:
            self._probe_in_flight = False
            if self._state == HALF_OPEN:
                # The probe failed: reopen immediately for another cycle.
                self._opened_at = now
                self._transition_locked(OPEN)
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._opened_at = now
                self._transition_locked(OPEN)

    # ----------------------------------------------------------------- test

    def _force_reset(self) -> None:
        """Reset to a fresh closed circuit (test seam)."""
        with self._lock:
            self._state = CLOSED
            self._consecutive_failures = 0
            self._probe_in_flight = False
            self._opened_at = 0.0


# One process-wide breaker per external dependency, rebuilt lazily when the
# configured thresholds change. Disabled configuration yields ``None`` so
# callers fall through to legacy behavior unchanged.
_breaker_lock = threading.Lock()
_breakers: dict[str, CircuitBreaker] = {}
_breaker_params: dict[str, tuple] = {}


def get_dependency_breaker(name: str, settings) -> CircuitBreaker | None:
    """Return the shared breaker for ``name``, or None when disabled."""
    if not getattr(settings, "enable_circuit_breakers", False):
        return None
    threshold = int(getattr(settings, "breaker_failure_threshold", 5))
    recovery = float(getattr(settings, "breaker_recovery_seconds", 60.0))
    params = (threshold, recovery)
    with _breaker_lock:
        if _breakers.get(name) is None or _breaker_params.get(name) != params:
            _breakers[name] = CircuitBreaker(name, threshold, recovery)
            _breaker_params[name] = params
        return _breakers[name]


def reset_breakers() -> None:
    """Drop all shared breakers (test seam)."""
    with _breaker_lock:
        _breakers.clear()
        _breaker_params.clear()
