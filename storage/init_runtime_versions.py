"""显式新增缺失的 P0 版本与溯源表；不随服务启动迁移数据库。"""
import os
from pathlib import Path

from dotenv import load_dotenv

from storage.database import make_db
from storage.runtime_versions import TABLES
from storage.research_provenance import TABLES as PROVENANCE_TABLES
from storage.coverage_reports import TABLES as COVERAGE_TABLES


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    engine, _ = make_db(os.environ['DATABASE_URL'])
    try:
        with engine.begin() as connection:
            for table in [*TABLES, *PROVENANCE_TABLES, *COVERAGE_TABLES]:
                table.create(connection, checkfirst=True)
        print('版本、溯源及覆盖报告表已准备：agent_versions, runtime_snapshots, research_interactions, research_plan_revisions, research_coverage_artifacts。现有任务记录未修改。')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
