"""Shared DOM work-budget instrumentation (issue #249).

Several modules independently traverse/serialize/text-scan the same document
(links, metadata, pruning, similar, adaptive, tables, extraction). Each pass
is individually bounded by input size, but one request can multiply a bounded
input into many unbounded O(n^2)-style passes. This module gives every
consumer ONE place to:

1. measure a parsed document once (node count + text chars, single pass), and
2. check a shared per-request work budget before doing another O(n) pass.

Usage::

    from sieve.dom_budget import DomWorkBudget

    budget = DomWorkBudget(total_nodes=200_000)
    if not budget.charge(len(tree)):
        return []          # budget exhausted: skip the optional pass

The budget is a plain object (no global state, no thread magic): callers who
own a request scope create one and pass it down. ``charge`` is monotonic —
exceeding the budget fails closed, it never waits or evicts.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["DomWorkBudget", "measure_tree", "DomMetrics"]

# Default per-request node budget: ~a 10MB HTML document's worth of nodes.
# Consumers doing OPTIONAL extra passes should charge against this and skip
# gracefully when exhausted; mandatory single passes need not budget at all.
DEFAULT_NODE_BUDGET = 2_000_000


@dataclass(frozen=True)
class DomMetrics:
    """One-pass structural measurements of a parsed document."""

    node_count: int
    text_chars: int
    max_depth: int

    @property
    def text_ratio(self) -> float:
        """Text chars per node; near-zero suggests a script/style-heavy DOM."""
        return self.text_chars / self.node_count if self.node_count else 0.0


def measure_tree(tree, budget=None) -> DomMetrics:
    """Measure each node once, with an iterator stack proportional to depth.

    Depth is relative to the supplied root. No ancestor walks or aggregate
    text strings are allocated. An optional shared account bounds the walk.
    """
    nodes = 0
    depth = 0
    roots = tree if isinstance(tree, (list, tuple)) else [tree]
    text_chars = 0
    for root in roots:
        stack = [(iter((root,)), 1)]
        while stack:
            iterator, level = stack[-1]
            element = next(iterator, None)
            if element is None:
                stack.pop()
                continue
            if budget is not None and not budget.charge(1):
                return DomMetrics(nodes, text_chars, depth)
            nodes += 1
            depth = max(depth, level)
            text_chars += len(element.text or "")
            if element is not root:
                text_chars += len(element.tail or "")
            stack.append((iter(element), level + 1))
    return DomMetrics(node_count=nodes, text_chars=text_chars, max_depth=depth)


@dataclass
class DomWorkBudget:
    """Charge-account for per-request DOM passes.

    Each ``charge(n)`` adds ``n`` units of work and returns whether the
    budget still has headroom. Fails closed: once exhausted, every charge
    returns False until the budget object is replaced.
    """

    total_nodes: int = DEFAULT_NODE_BUDGET
    used: int = field(default=0, init=False)
    exhausted: bool = field(default=False, init=False)

    def __post_init__(self):
        if isinstance(self.total_nodes, bool) or not isinstance(self.total_nodes, int) or self.total_nodes < 0:
            raise ValueError("node budget must be a nonnegative integer")

    @property
    def remaining(self) -> int:
        return max(0, self.total_nodes - self.used)

    def charge(self, nodes: int) -> bool:
        """Record ``nodes`` of work. Returns False when the budget is spent."""
        if self.exhausted:
            return False
        if isinstance(nodes, bool) or not isinstance(nodes, int) or nodes < 0:
            raise ValueError("node charges must be nonnegative integers")
        from sieve.resource_budget import current_budget
        account = current_budget()
        if self.used + nodes > self.total_nodes or (account is not None and not account.charge("nodes", nodes)):
            self.exhausted = True
            if account is not None:
                account.truncated.add("nodes")
            return False
        self.used += nodes
        return True

    def would_exceed(self, nodes: int) -> bool:
        return self.exhausted or self.used + nodes > self.total_nodes


def dom_budget(budget=None):
    return budget if budget is not None else DomWorkBudget()


def allow_html_pass(html: str, budget) -> bool:
    """Charge an input-size upper bound before parsing/materializing a pass."""
    return budget.charge(len(html) + 1)
