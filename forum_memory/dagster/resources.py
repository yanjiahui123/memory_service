"""Dagster resources — DB session and LLM provider."""

from dagster import ConfigurableResource
from sqlmodel import Session

from forum_memory.database import engine


class DBResource(ConfigurableResource):
    """Provides a SQLModel Session for each op invocation."""

    def get_session(self) -> Session:
        return Session(engine)
