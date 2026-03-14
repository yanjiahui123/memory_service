"""SourceAdapter — abstract interface for knowledge sources.

Each source type (forum thread, ticket, Q&A pair, document, etc.) implements
this interface.  The adapter knows how to load source data and transform it
into a generic SourceContext that the extraction pipeline can consume.
"""

from abc import ABC, abstractmethod
from uuid import UUID

from sqlmodel import Session

from forum_memory.core.source_context import SourceContext


class SourceAdapter(ABC):
    """Base class for source adapters.

    Concrete adapters must implement four methods:
    - source_type(): identifier string
    - event_types(): DomainEvent types that trigger extraction
    - load_context(): build a SourceContext from source_id
    - lock_for_re_extract(): row-level lock for concurrent protection
    """

    @abstractmethod
    def source_type(self) -> str:
        """Return the source_type string, e.g. 'thread', 'ticket'."""

    @abstractmethod
    def event_types(self) -> tuple[str, ...]:
        """DomainEvent.event_type values that should trigger extraction."""

    @abstractmethod
    def load_context(self, session: Session, source_id: UUID) -> SourceContext | None:
        """Load the source entity and build a SourceContext.

        Returns None if the source is not found or not ready for extraction.
        """

    @abstractmethod
    def lock_for_re_extract(self, session: Session, source_id: UUID) -> None:
        """Acquire a row-level lock to prevent concurrent re-extraction.

        Implementations should use SELECT ... FOR UPDATE NOWAIT.
        """
