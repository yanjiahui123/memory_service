"""Database engine and session management (synchronous)."""

from sqlmodel import SQLModel, Session, create_engine
from forum_memory.config import get_settings

engine = create_engine(
    get_settings().database_url,
    echo=get_settings().database_echo,
    pool_pre_ping=True,  # 自动检测断开的连接
    pool_size=5,
    max_overflow=10,
    pool_timeout=10,  # 获取连接的超时时间（秒）
    connect_args={
        "connect_timeout": 5,  # psycopg2 连接超时 5 秒，防止无限挂起
    },
)


def get_session():
    """FastAPI dependency — yields a sync Session."""
    with Session(engine) as session:
        yield session


def init_db():
    """Create all tables."""
    SQLModel.metadata.create_all(engine)