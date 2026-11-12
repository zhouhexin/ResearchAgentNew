
import os

from dotenv import load_dotenv
from sqlalchemy import text

from storage.database import make_db


def main():
    load_dotenv()

    database_url = os.environ["DATABASE_URL"]

    engine, _ = make_db(database_url)

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT
                        current_database(),
                        current_user,
                        current_timestamp
                """)
            ).one()

            print("PostgreSQL 连接成功！")
            print("数据库：", result[0])
            print("用户：", result[1])
            print("服务器时间：", result[2])

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
