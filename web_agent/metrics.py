"""v1.7.0 (Wave 4A): lightweight in-process metrics registry.

A long-running MCP daemon needs to answer "how many fetches, how many got
bot-walled, how many browser crashes/relaunches, which search providers are
blocked, what's my error rate" WITHOUT grepping logs. This module is that
surface -- a cheap, allocation-light, asyncio-friendly counter + distribution
registry that the hot paths increment at outcome points only.

Design constraints (deliberately minimal):

* **Counters** -- monotonically increasing integers keyed by ``(name,
  labels)``. ``incr(name, value=1, **labels)``.
* **Distributions** -- a four-number summary (count / sum / min / max) per
  ``(name, labels)`` series. NO histogram buckets -- those allocate and the
  callers here only need rough shape (bytes downloaded, TTFB). ``observe(
  name, value, **labels)``.
* **Cardinality guard** -- a hostile or naive high-cardinality label (per-URL
  host, per-correlation-id) must not grow the registry unbounded. Each metric
  caps the number of distinct label-combinations it tracks
  (``max_label_cardinality``); once the cap is hit, further label-combos fold
  into a single ``{...: "_other"}`` overflow bucket so the registry size is
  bounded regardless of input.
* **Single-threaded asyncio** -- the event loop runs callbacks one at a time,
  so a plain dict needs no lock for correctness on the asyncio path. The
  mutations here are individually non-awaiting (no ``await`` between read and
  write), so they are atomic with respect to other coroutines.
* **Disabled == near-zero cost** -- a disabled registry's ``incr`` / ``observe``
  return immediately after one boolean check; nothing is allocated and the
  snapshot simply reports ``enabled=False`` with empty maps.

The module exposes a process-wide default registry (``default_registry()`` /
the module-level functions) AND lets the Agent own its own instance so two
Agents in one process don't share counters. Instrumentation code calls the
registry it is handed (constructors thread an optional ``metrics`` param that
defaults to a shared no-op when omitted, so existing call sites are
unaffected).
"""

from __future__ import annotations

import time
from typing import Optional

# Sentinel folded into a series' label set once a metric exceeds its
# cardinality cap. A single overflow bucket bounds registry growth no matter
# how many distinct label-combos the callers throw at it.
_OVERFLOW_LABEL_KEY = "__overflow__"
_OVERFLOW_LABEL_VALUE = "_other"


def _format_series_key(name: str, labels: dict[str, str]) -> str:
    """Render a stable ``name{k=v,k2=v2}`` series key for snapshots.

    Labels are sorted so the key is deterministic regardless of kwarg order
    (``incr("x", a=1, b=2)`` and ``incr("x", b=2, a=1)`` hit the same series).
    A label-free metric renders as the bare ``name`` (no empty braces).
    """
    if not labels:
        return name
    inner = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{inner}}}"


class _Distribution:
    """A cheap four-number summary: count / sum / min / max.

    No buckets, no reservoir -- just the running aggregates the callers here
    need. ``avg`` is derived on snapshot (``sum / count``) rather than stored.
    """

    __slots__ = ("count", "max", "min", "sum")

    def __init__(self) -> None:
        self.count: int = 0
        self.sum: float = 0.0
        self.min: float = 0.0
        self.max: float = 0.0

    def observe(self, value: float) -> None:
        if self.count == 0:
            self.min = value
            self.max = value
        else:
            if value < self.min:
                self.min = value
            if value > self.max:
                self.max = value
        self.count += 1
        self.sum += value

    def as_dict(self) -> dict[str, float]:
        avg = self.sum / self.count if self.count else 0.0
        return {
            "count": float(self.count),
            "sum": self.sum,
            "min": self.min,
            "max": self.max,
            "avg": avg,
        }


class MetricsRegistry:
    """In-process counters + distributions with a bounded label cardinality.

    Args:
        enabled: When False, ``incr`` / ``observe`` are no-ops (one boolean
            check, no allocation) and ``snapshot`` reports empty maps with
            ``enabled=False``. The whole point of the registry is to HAVE a
            surface, so the default is enabled; disabling exists for callers
            that want to pay nothing.
        max_label_cardinality: Maximum distinct label-combinations tracked per
            metric name. Once a metric reaches this many series, any new
            label-combo folds into a single ``{__overflow__: "_other"}`` bucket
            instead of adding another series -- so a per-host / per-URL label
            cannot grow the registry without bound. Must be >= 1.
    """

    def __init__(self, *, enabled: bool = True, max_label_cardinality: int = 200) -> None:
        self._enabled = enabled
        self._max_cardinality = max(1, int(max_label_cardinality))
        # series-key (str) -> int / _Distribution. The series-key encodes
        # the resolved labels (post cardinality-fold) so snapshot() is a
        # plain copy + format with no per-call key building.
        self._counters: dict[str, int] = {}
        self._distributions: dict[str, _Distribution] = {}
        # name -> set of series-keys seen for that metric, used only to
        # enforce the cardinality cap. Kept separate from the value maps so
        # the cap is per-metric-name, not per-(name,labels).
        self._counter_series: dict[str, set[str]] = {}
        self._dist_series: dict[str, set[str]] = {}
        self._start_time = time.monotonic()

    @property
    def enabled(self) -> bool:
        """True when increments are recorded; False when they are no-ops."""
        return self._enabled

    def _resolve_key(
        self,
        name: str,
        labels: dict[str, str],
        seen: dict[str, set[str]],
    ) -> str:
        """Build the series key for ``(name, labels)``, applying the cap.

        ``seen`` is the per-kind (counter vs distribution) name->series-keys
        map. When the metric already has ``max_label_cardinality`` distinct
        series and this combo is new, the labels are replaced with the single
        overflow bucket so registry growth is bounded.
        """
        key = _format_series_key(name, labels)
        series = seen.get(name)
        if series is None:
            series = set()
            seen[name] = series
        if key in series:
            return key
        if len(series) < self._max_cardinality:
            series.add(key)
            return key
        # Cap reached: fold this (and every future new combo) into one bucket.
        overflow_key = _format_series_key(name, {_OVERFLOW_LABEL_KEY: _OVERFLOW_LABEL_VALUE})
        series.add(overflow_key)
        return overflow_key

    def incr(self, name: str, value: int = 1, **labels: str) -> None:
        """Add ``value`` to the counter ``name`` for the given label set.

        No-op when the registry is disabled. Labels are coerced to ``str`` so
        a stray int/enum value can't make two series collide differently from
        the snapshot rendering.
        """
        if not self._enabled:
            return
        str_labels = {k: str(v) for k, v in labels.items()}
        key = self._resolve_key(name, str_labels, self._counter_series)
        self._counters[key] = self._counters.get(key, 0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record ``value`` into the distribution ``name`` for the label set.

        Tracks count / sum / min / max. No-op when disabled.
        """
        if not self._enabled:
            return
        str_labels = {k: str(v) for k, v in labels.items()}
        key = self._resolve_key(name, str_labels, self._dist_series)
        dist = self._distributions.get(key)
        if dist is None:
            dist = _Distribution()
            self._distributions[key] = dist
        dist.observe(float(value))

    def uptime_s(self) -> float:
        """Seconds since this registry was constructed (or last ``reset``)."""
        return max(0.0, time.monotonic() - self._start_time)

    def snapshot(self) -> dict[str, object]:
        """Return a plain, JSON-trivial dict snapshot of all metrics.

        Shape::

            {
                "enabled": bool,
                "uptime_s": float,
                "counters": {series_key: int, ...},
                "distributions": {series_key: {count, sum, min, max, avg}, ...},
            }

        Cheap to call: copies the counter ints and renders each distribution's
        five-number dict. ``series_key`` is ``name`` or ``name{k=v,...}``.
        """
        return {
            "enabled": self._enabled,
            "uptime_s": self.uptime_s(),
            "counters": dict(self._counters),
            "distributions": {key: dist.as_dict() for key, dist in self._distributions.items()},
        }

    def reset(self) -> None:
        """Clear all counters/distributions and restart the uptime clock.

        Intended for tests; not used on any hot path.
        """
        self._counters.clear()
        self._distributions.clear()
        self._counter_series.clear()
        self._dist_series.clear()
        self._start_time = time.monotonic()


# ---------------------------------------------------------------------------
# Module-level default registry
# ---------------------------------------------------------------------------

# Process-wide default. Instrumentation that isn't handed an explicit registry
# falls back to this one. The Agent owns its OWN instance (constructor param)
# so two Agents in one process don't share counters -- this default is the
# safety net for direct module use and the no-op fallback target.
_DEFAULT_REGISTRY = MetricsRegistry()

# A shared, permanently-disabled registry used as the default constructor
# argument in instrumented modules. Threading THIS (rather than None) keeps
# every increment a single cheap call with no ``if self._metrics is not None``
# guard scattered through the hot paths.
_NOOP_REGISTRY = MetricsRegistry(enabled=False)


def default_registry() -> MetricsRegistry:
    """Return the process-wide default :class:`MetricsRegistry`."""
    return _DEFAULT_REGISTRY


def noop_registry() -> MetricsRegistry:
    """Return the shared permanently-disabled registry (near-zero cost).

    Used as the default ``metrics=`` argument in instrumented constructors so
    existing call sites that don't pass a registry pay nothing per increment.
    """
    return _NOOP_REGISTRY


def get_metrics(metrics: Optional[MetricsRegistry]) -> MetricsRegistry:
    """Normalize an optional registry to a concrete one.

    Returns ``metrics`` when not None, else the shared no-op registry. Lets a
    constructor write ``self._metrics = get_metrics(metrics)`` once and then
    call ``self._metrics.incr(...)`` unconditionally on every outcome path.
    """
    return metrics if metrics is not None else _NOOP_REGISTRY
