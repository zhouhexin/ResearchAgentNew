
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """所有数据库 ORM 模型的基类。"""
    pass


def make_db(database_url: str):
    """创建数据库引擎和 Session 工厂。"""

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args=(
            {"connect_timeout": 5}
            if make_url(database_url).get_backend_name() == "postgresql"
            else {}
        ),
    )

    session_factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )

    return engine, session_factory
