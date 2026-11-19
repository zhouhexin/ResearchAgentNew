"""可选全量检查产物隔离；仅通过 -p test.audit_isolation 显式加载。"""
import pytest

@pytest.fixture(autouse=True)
def audit_output_isolation(monkeypatch,tmp_path):
    from api import tasks, uploads, analyses, reviews, claim_reviews
    import agent.runtime_snapshot as runtime
    monkeypatch.setattr(tasks,'TASK_DB_PATH',tmp_path/'audit_tasks.sqlite3')
    monkeypatch.setattr(uploads,'UPLOAD_ROOT',tmp_path/'audit_uploads')
    monkeypatch.setattr(analyses,'ERROR_DIR',tmp_path/'analysis_errors')
    monkeypatch.setattr(reviews,'ERROR_DIR',tmp_path/'review_errors')
    monkeypatch.setattr(claim_reviews,'RUN_ROOT',tmp_path/'claim_reviews')
    monkeypatch.setattr(runtime,'IDENTITY_KEY',tmp_path/'runtime_identity.key')
