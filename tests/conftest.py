"""Test fixtures: in-memory SQLite engine, session, and model factories."""

import os

# Set dummy env vars BEFORE any forum_memory imports that trigger get_settings().
# The lru_cache on get_settings() means the first call wins.
os.environ.setdefault("FM_DATABASE_URL", "sqlite://")
os.environ.setdefault("FM_LLM_API_KEY", "test-key")
os.environ.setdefault("FM_LLM_PROVIDER", "custom")
os.environ.setdefault("FM_CUSTOM_LLM_URL", "http://localhost:1")
os.environ.setdefault("FM_CUSTOM_EMBED_URL", "http://localhost:1")
os.environ.setdefault("FM_CUSTOM_RERANK_URL", "http://localhost:1")
os.environ.setdefault("FM_SSO_ENABLED", "false")
os.environ.setdefault("FM_SSO_VERIFY_URL", "http://localhost:1")
os.environ.setdefault("FM_SSO_AK", "test-ak")
os.environ.setdefault("FM_SSO_SK", "test-sk")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from sqlmodel import SQLModel, Session, create_engine  # noqa: E402


# ---------------------------------------------------------------------------
# Database engine & session
# ---------------------------------------------------------------------------

@pytest.fixture(name="db_engine")
def fixture_db_engine():
    """Create an in-memory SQLite engine with all tables."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    # Import all models so metadata is populated
    import forum_memory.models  # noqa: F401
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(name="session")
def fixture_session(db_engine):
    """Yield a fresh session per test."""
    with Session(db_engine) as sess:
        yield sess


# ---------------------------------------------------------------------------
# Factory fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(name="user_factory")
def fixture_user_factory(session):
    """Factory for User records."""
    counter = 0

    def _make(**overrides):
        nonlocal counter
        counter += 1
        from forum_memory.models.user import User

        defaults = {
            "employee_id": f"test{counter:04d}",
            "username": f"tester_{uuid4().hex[:6]}",
            "display_name": f"Test User {counter}",
        }
        defaults.update(overrides)
        user = User(**defaults)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    return _make


@pytest.fixture(name="namespace_factory")
def fixture_namespace_factory(session, user_factory):
    """Factory for Namespace records (auto-creates owner User)."""
    def _make(**overrides):
        from forum_memory.models.namespace import Namespace

        owner = overrides.pop("owner", None) or user_factory()
        defaults = {
            "name": f"test_ns_{uuid4().hex[:8]}",
            "display_name": "Test Board",
            "owner_id": owner.id,
        }
        defaults.update(overrides)
        ns = Namespace(**defaults)
        session.add(ns)
        session.commit()
        session.refresh(ns)
        return ns
    return _make


@pytest.fixture(name="memory_factory")
def fixture_memory_factory(session, namespace_factory):
    """Factory for Memory records (auto-creates Namespace)."""
    def _make(**overrides):
        from forum_memory.models.memory import Memory

        ns = overrides.pop("namespace", None) or namespace_factory()
        defaults = {
            "namespace_id": ns.id,
            "content": f"Test memory content {uuid4().hex[:6]}",
            "quality_score": 0.5,
        }
        defaults.update(overrides)
        mem = Memory(**defaults)
        session.add(mem)
        session.commit()
        session.refresh(mem)
        return mem
    return _make
