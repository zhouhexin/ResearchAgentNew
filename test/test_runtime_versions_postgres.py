"""可选真实 PostgreSQL 验收；沿用仅 researchpilot_test / 临时 schema 的保护。"""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from agent.runtime_snapshot import capture_runtime
from storage.runtime_versions import RuntimeSnapshotRecord, load_immutable, save_immutable


def test_postgres_jsonb_concurrent_idempotence_and_readback(isolated_database_url, monkeypatch, tmp_path):
    monkeypatch.setattr('agent.runtime_snapshot.IDENTITY_KEY', tmp_path / 'identity.key')
    monkeypatch.setenv('LLM_API_KEY', 'isolated-test-credential')
    monkeypatch.setenv('LLM_MODEL', 'isolated-test-model')
    monkeypatch.setenv('LLM_BASE_URL', 'https://example.invalid/v1')
    snapshot = capture_runtime(read_timeout=10, trust_env=False, http_backend='sdk')
    engine = create_engine(isolated_database_url)
    try:
        def save(_):
            with Session(engine) as session, session.begin():
                return save_immutable(session, snapshot)
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(save, range(2))) == [snapshot.id, snapshot.id]
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(RuntimeSnapshotRecord)) == 1
            assert load_immutable(session, snapshot.id) == snapshot
    finally:
        engine.dispose()
