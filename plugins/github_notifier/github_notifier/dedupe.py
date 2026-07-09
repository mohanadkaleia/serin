"""Bounded delivery-id dedupe (M5 Q6): the idempotency the hook endpoint lacks.

GitHub delivers at-least-once (redeliveries share the ``X-GitHub-Delivery``
GUID), and msg's ``POST /v1/hooks/{token}`` has no idempotency key — so the
plugin dedupes on its side. A bounded in-memory LRU is enough for a reference
plugin: redeliveries arrive within minutes, and losing the log on restart only
risks a duplicate message, never a lost one.
"""

from __future__ import annotations

from collections import OrderedDict

__all__ = ["DeliveryLog"]


class DeliveryLog:
    """An LRU set of delivery ids already POSTed (successfully) to the hook."""

    def __init__(self, capacity: int = 1024) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, delivery_id: str) -> bool:
        if delivery_id in self._seen:
            self._seen.move_to_end(delivery_id)
            return True
        return False

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, delivery_id: str) -> None:
        """Record ``delivery_id``, evicting the least-recently-seen over capacity."""
        self._seen[delivery_id] = None
        self._seen.move_to_end(delivery_id)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
