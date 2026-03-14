"""SourceContext — unified representation of an extractable source.

This is the contract between SourceAdapters and the extraction pipeline.
Adapters produce a SourceContext; the pipeline consumes it.
"""

from dataclasses import dataclass
from uuid import UUID

from forum_memory.models.enums import Authority


@dataclass(frozen=True)
class SourceContext:
    """Immutable snapshot of a source ready for knowledge extraction."""

    # Identity
    source_type: str          # "thread", "ticket", "qa", ...
    source_id: UUID           # primary key of the source record
    namespace_id: UUID        # which namespace it belongs to

    # Content (what the pipeline consumes)
    title: str                # source title or summary
    question: str             # primary question / content body
    discussion: str           # pre-formatted discussion text

    # Extraction policy (determined by the adapter)
    authority: Authority      # default authority for created memories
    pending_human_confirm: bool  # whether memories need human review

    # Source metadata (passed through to Memory records)
    environment: str | None = None
    source_role: str | None = None      # e.g. "expert", "ai", "unknown"
    resolved_type: str | None = None    # e.g. "human_resolved", "ai_resolved"
