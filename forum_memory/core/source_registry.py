"""Source adapter registry — register and look up adapters at runtime."""

from forum_memory.core.source_adapter import SourceAdapter

_adapters: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    """Register a source adapter by its source_type (idempotent)."""
    stype = adapter.source_type()
    if stype in _adapters:
        return  # Already registered — safe to skip on repeated import
    _adapters[stype] = adapter


def get_adapter(source_type: str) -> SourceAdapter:
    """Look up an adapter by source_type. Raises KeyError if not found."""
    if source_type not in _adapters:
        raise KeyError(f"No adapter registered for source_type '{source_type}'")
    return _adapters[source_type]


def all_event_types() -> list[str]:
    """Collect all event types from all registered adapters."""
    result: list[str] = []
    for adapter in _adapters.values():
        result.extend(adapter.event_types())
    return result


def adapter_for_event(event_type: str) -> SourceAdapter | None:
    """Find which adapter handles a given event_type."""
    for adapter in _adapters.values():
        if event_type in adapter.event_types():
            return adapter
    return None
