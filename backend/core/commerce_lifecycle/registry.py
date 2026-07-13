"""
Read-only LifecycleIntentRegistry — no I/O, no automation, no AI imports.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Iterator, Tuple

from core.commerce_lifecycle.definitions import (
    INITIAL_DEFINITIONS,
    BusinessIntentDefinition,
)
from core.commerce_lifecycle.intents import BusinessIntent


class LifecycleIntentRegistry:
    """Maps each BusinessIntent to exactly one immutable definition."""

    def __init__(self, definitions: Tuple[BusinessIntentDefinition, ...] = ()) -> None:
        self._by_intent: Dict[BusinessIntent, BusinessIntentDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: BusinessIntentDefinition) -> None:
        if definition.intent in self._by_intent:
            raise ValueError(
                f"duplicate BusinessIntent registration: {definition.intent.value}"
            )
        self._by_intent[definition.intent] = definition

    def get(self, intent: BusinessIntent) -> BusinessIntentDefinition:
        try:
            return self._by_intent[intent]
        except KeyError as exc:
            raise KeyError(f"unsupported BusinessIntent: {intent.value}") from exc

    def try_get(self, intent: BusinessIntent) -> BusinessIntentDefinition | None:
        return self._by_intent.get(intent)

    def has(self, intent: BusinessIntent) -> bool:
        return intent in self._by_intent

    def list_definitions(self) -> Tuple[BusinessIntentDefinition, ...]:
        return tuple(self._by_intent[intent] for intent in sorted(self._by_intent, key=lambda i: i.value))

    def registered_intents(self) -> FrozenSet[BusinessIntent]:
        return frozenset(self._by_intent.keys())

    def __iter__(self) -> Iterator[BusinessIntentDefinition]:
        return iter(self.list_definitions())

    def __len__(self) -> int:
        return len(self._by_intent)


_DEFAULT_REGISTRY: LifecycleIntentRegistry | None = None


def get_default_registry() -> LifecycleIntentRegistry:
    """Return the process-wide registry seeded with INITIAL_DEFINITIONS."""
    global _DEFAULT_REGISTRY  # noqa: PLW0603
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = LifecycleIntentRegistry(INITIAL_DEFINITIONS)
    return _DEFAULT_REGISTRY


__all__ = ["LifecycleIntentRegistry", "get_default_registry"]
