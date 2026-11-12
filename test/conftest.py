"""数据库测试只使用唯一临时 schema，不清空既有表。"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateSchema, DropSchema

from storage.database import Base
import storage.db_models


@pytest.fixture(autouse=True)
def isolated_model_provider(monkeypatch):
    # 不因开发者切换 .env 而让受控模型测试转向真实 Codex 登录。
    monkeypatch.setenv('LLM_PROVIDER', 'openai')


@pytest.fixture
def isolated_database_url():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("请配置 TEST_DATABASE_URL")
    url = make_url(value)
    if url.get_backend_name() != "postgresql" or url.database != "researchpilot_test":
        pytest.fail("集成测试仅允许 researchpilot_test PostgreSQL 数据库", pytrace=False)
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    schema = "rp_test_" + uuid4().hex
    created = False
    try:
        connection_failed = False
        try:
            with admin.begin() as conn:
                conn.execute(CreateSchema(schema))
            created = True
        except SQLAlchemyError:
            connection_failed = True
        if connection_failed:
            pytest.fail("无法连接测试 PostgreSQL 或创建临时 schema；检查网络权限及配置", pytrace=False)
        options = f"{url.query.get('options', '')} -csearch_path={schema}".strip()
        isolated_url = url.update_query_dict({"options": options})
        engine = create_engine(isolated_url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        yield isolated_url.render_as_string(hide_password=False)
    finally:
        try:
            if created:
                with admin.begin() as conn:
                    conn.execute(DropSchema(schema, cascade=True))
        finally:
            admin.dispose()
