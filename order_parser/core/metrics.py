"""Lightweight in-process metrics (Phase 8).

A dependency-free, thread-safe registry exposing Prometheus text format via
/metrics. Counters and simple observations (count/sum/max) only - enough for
operational dashboards without pulling in a client library. Single-process
scope matches the local-store design of this service.
"""
from __future__ import annotations

import threading
import time


def _label_key(labels: dict) -> tuple:
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(labels: tuple) -> str:
    if not labels:
        return ""
    pairs = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + pairs + "}"


def _format(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}"


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = {}
        self._observations: dict[tuple[str, tuple], dict[str, float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._observations.clear()

    # ------------------------------------------------------------------ write

    def incr(self, name: str, value: float = 1.0, **labels) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + float(value)

    def counter_value(self, name: str, **labels) -> float:
        with self._lock:
            return self._counters.get((name, _label_key(labels)), 0.0)

    def observe(self, name: str, seconds: float, **labels) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            stats = self._observations.setdefault(key, {"count": 0.0, "sum": 0.0, "max": 0.0})
            stats["count"] += 1
            stats["sum"] += float(seconds)
            stats["max"] = max(stats["max"], float(seconds))

    def observation(self, name: str, **labels) -> dict[str, float] | None:
        with self._lock:
            stats = self._observations.get((name, _label_key(labels)))
            return dict(stats) if stats else None

    # ------------------------------------------------------------------ render

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = {k: v for k, v in self._counters.items()}
            observations = {k: dict(v) for k, v in self._observations.items()}

        by_name: dict[str, list[tuple[tuple, float]]] = {}
        for (name, labels), value in counters.items():
            by_name.setdefault(name, []).append((labels, value))
        for name in sorted(by_name):
            lines.append(f"# TYPE {name} counter")
            for labels, value in sorted(by_name[name]):
                lines.append(f"{name}{_render_labels(labels)} {_format(value)}")

        obs_by_name: dict[str, list[tuple[tuple, dict[str, float]]]] = {}
        for (name, labels), stats in observations.items():
            obs_by_name.setdefault(name, []).append((labels, stats))
        for name in sorted(obs_by_name):
            lines.append(f"# TYPE {name} summary")
            for labels, stats in sorted(obs_by_name[name]):
                rendered = _render_labels(labels)
                lines.append(f'{name}_sum{rendered} {_format(stats["sum"])}')
                lines.append(f'{name}_count{rendered} {_format(stats["count"])}')
                lines.append(f'{name}_max{rendered} {_format(stats["max"])}')
        return "\n".join(lines) + ("\n" if lines else "")


REGISTRY = MetricsRegistry()


def incr(name: str, value: float = 1.0, **labels) -> None:
    REGISTRY.incr(name, value, **labels)


def observe(name: str, seconds: float, **labels) -> None:
    REGISTRY.observe(name, seconds, **labels)


def monotonic() -> float:
    return time.monotonic()
