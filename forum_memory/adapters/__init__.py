"""Source adapters — register all adapters on import."""

from forum_memory.core.source_registry import register_adapter
from forum_memory.adapters.thread_adapter import ThreadSourceAdapter

register_adapter(ThreadSourceAdapter())
