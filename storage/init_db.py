"""创建缺失的数据表：python -m storage.init_db。"""

import os
from pathlib import Path

from dotenv import load_dotenv

from storage.database import Base, make_db
import storage.db_models  # 注册所有 ORM 表


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    engine, _ = make_db(os.environ["DATABASE_URL"])
    try:
        Base.metadata.create_all(engine)
        from storage.venue_repository import seed_venue
        from tools.venue_search.catalog import VENUES
        from sqlalchemy.orm import Session
        with Session(engine) as session, session.begin():
            for slug in VENUES:
                seed_venue(session, slug)
        print("数据库表已准备：" + ", ".join(sorted(Base.metadata.tables)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
