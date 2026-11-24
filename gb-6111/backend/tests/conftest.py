import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

# 测试前补齐应用启动所需的环境变量，便于在本地/CI 直接指向 PostgreSQL。
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://workbench@/workbench?host=/tmp/pgtest&port=55432",
)
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DEMO_PASSWORD", "test-password")
os.environ.setdefault("SEED_DEMO", "false")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")

from scripts.seed_demo import setup_schema  # noqa: E402
from src.config.resources import engine  # noqa: E402


@pytest.fixture(scope="session")
def db_engine():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - 无数据库时跳过
        pytest.skip(f"PostgreSQL 不可用：{exc}")
    # 归档临时表是连接级对象（ON COMMIT DROP），且 DROP/CREATE SCHEMA 这类
    # DDL 对未提交事务持锁敏感。测试统一改用 NullPool：连接关闭即物理断开，
    # 不会把 idle-in-transaction 连接还回池里造成 DDL 互锁。
    test_engine = create_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    with test_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        # 在同一条连接、同一事务里完成建表与迁移，避免跨引擎连接持锁互斥。
        setup_schema(conn)
    yield test_engine


@pytest.fixture()
def db(db_engine):
    # 整个测试固定在同一条连接上：测试用例与应用代码共享连接，临时表互相可见；
    # 测试结束统一回滚，避免污染后续用例。
    conn = db_engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        trans.rollback()
        conn.close()
