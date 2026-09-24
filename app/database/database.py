"""
数据库连接模块（SQLite）

说明：
- 数据库文件保存在项目根目录 data/fundpilot.db（该目录不会提交 GitHub）
- Phase 3 只有一张持仓表，规模很小，直接用 SQLAlchemy 建表即可
- 后续迁移 MySQL 时只需要改 DATABASE_URL，其余代码不用动
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# 数据库文件位置：项目根目录/data/fundpilot.db
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'fundpilot.db')}")

# SQLAlchemy 引擎
# check_same_thread=False：允许多线程访问 SQLite（FastAPI 多线程环境下需要）
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# 会话工厂：每次数据库操作创建一个会话，用完即关
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类（SQLAlchemy 2.x 写法）"""


def init_db() -> None:
    """创建所有尚未存在的数据表（已存在则跳过）"""
    # 导入模型模块，确保所有表都已注册到 Base.metadata
    from app.database import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI 依赖：提供数据库会话，请求结束后自动关闭"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
