"""Request-owned aggregate limits, shared by nested and concurrent work.

Transport decompression and parser-specific limits remain responsible for
pre-allocation safety. This account limits the sum of otherwise bounded work.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from dataclasses import dataclass, field
from functools import wraps
from math import isfinite
from time import monotonic
from pydantic import BaseModel


DEFAULT_LIMITS = {
    "input_bytes": 50_000_000,
    "output_chars": 500_000,
    "items": 10_000,
    "pages": 100,
    "nodes": 2_000_000,
    "retries": 30,
}


class BudgetExceeded(ValueError):
    category = "budget"


@dataclass
class ResourceBudget:
    limits: dict[str, int] = field(default_factory=lambda: DEFAULT_LIMITS.copy())
    timeout_seconds: float = 120.0
    consumed: dict[str, int] = field(default_factory=dict, init=False)
    truncated: set[str] = field(default_factory=set, init=False)
    _started: float = field(default_factory=monotonic, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self):
        if set(self.limits) - DEFAULT_LIMITS.keys():
            raise ValueError("unknown resource dimension")
        self.limits = {**DEFAULT_LIMITS, **self.limits}
        for value in self.limits.values():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("resource limits must be nonnegative integers")
        if (isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not isfinite(self.timeout_seconds) or self.timeout_seconds < 0):
            raise ValueError("timeout must be finite and nonnegative")

    @property
    def time_remaining(self) -> float:
        return max(0.0, self.timeout_seconds - (monotonic() - self._started))

    @property
    def expired(self) -> bool:
        if monotonic() - self._started >= self.timeout_seconds:
            self.truncated.add("deadline")
            return True
        return False

    def remaining(self, dimension: str) -> int:
        return self.limits[dimension] - self.consumed.get(dimension, 0)

    def take(self, dimension: str, amount: int) -> int:
        """Reserve up to amount before allocating or emitting that work."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("resource charges must be nonnegative integers")
        with self._lock:
            available = self.remaining(dimension)
            if self.expired:
                self.truncated.add(dimension)
                return 0
            allowed = min(amount, available)
            self.consumed[dimension] = self.consumed.get(dimension, 0) + allowed
            if allowed < amount:
                self.truncated.add(dimension)
            return allowed

    def charge(self, dimension: str, amount: int) -> bool:
        return self.take(dimension, amount) == amount and not self.expired

    def report(self) -> dict:
        if self.time_remaining == 0:
            self.truncated.add("deadline")
        return {"consumed": dict(self.consumed), "truncated": sorted(self.truncated)}


_CURRENT: ContextVar[ResourceBudget | None] = ContextVar("sieve_resource_budget", default=None)
_OUTPUT_OWNER: ContextVar[object | None] = ContextVar("sieve_output_owner", default=None)


def current_budget() -> ResourceBudget | None:
    return _CURRENT.get()


@contextmanager
def budget_scope(budget: ResourceBudget | None = None):
    """Nested operations share their caller's account; roots create one."""
    account = budget if budget is not None else _CURRENT.get() or ResourceBudget()
    token = _CURRENT.set(account)
    try:
        yield account
    finally:
        _CURRENT.reset(token)


def budgeted(function):
    """Keep internal account propagation out of public tool definitions."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with budget_scope():
            return await function(*args, **kwargs)
    return wrapped


def budgeted_collection(function):
    """Let the outermost collector account for payloads before storing/emitting."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with budget_scope():
            token = _OUTPUT_OWNER.set(_OUTPUT_OWNER.get() or wrapped)
            try:
                return await function(*args, **kwargs)
            finally:
                _OUTPUT_OWNER.reset(token)
    return wrapped


def bound_output(value, *, owner=None, depth=0):
    """Copy a JSON payload within shared character/item limits, once per collector.

    Envelope metadata is added by the caller after payload accounting. Nested
    collectors defer to their owner, avoiding duplicate charges for one payload.
    """
    account = current_budget()
    if account is None or (_OUTPUT_OWNER.get() is not None and _OUTPUT_OWNER.get() is not owner):
        return value
    if depth > 64:
        account.truncated.add("nodes")
        return None
    if isinstance(value, str):
        return value[:account.take("output_chars", len(value))]
    if isinstance(value, BaseModel):
        value = {name: getattr(value, name) for name in type(value).model_fields}
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str) or not account.charge("output_chars", len(key)):
                break
            result[key] = bound_output(child, owner=owner, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for child in value:
            if not account.charge("items", 1):
                break
            result.append(bound_output(child, owner=owner, depth=depth + 1))
        return result
    return value
