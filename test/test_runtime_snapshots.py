"""P0 实际请求隔离与不可变内容仓库；仅使用 MockTransport、临时文件/数据库。"""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
import json
import subprocess

from openai import DefaultHttpxClient
from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from agent.agent_version import AgentVersion, pinned_llm_factory
from agent.llm_runtime import analysis_llm
from agent.runtime_snapshot import (
    RuntimeSnapshot, RuntimeUnavailable, capture_runtime, safe_manifest,
)
from storage.runtime_versions import (
    AgentVersionRecord, RuntimeSnapshotRecord, TABLES, load_immutable, save_immutable,
)


@pytest.fixture
def environment(monkeypatch, tmp_path):
    import agent.runtime_snapshot as runtime
    monkeypatch.setattr(runtime, 'IDENTITY_KEY', tmp_path / 'identity.key')
    for name in (*runtime.NETWORK_KEYS, 'LLM_ENABLE_THINKING', 'LLM_MAX_OUTPUT_TOKENS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LLM_PROVIDER', 'openai')
    monkeypatch.setenv('LLM_MODEL', 'model-A')
    monkeypatch.setenv('LLM_API_KEY', 'test-only-private-credential')
    monkeypatch.setenv('LLM_BASE_URL', 'https://a.example/v1')
    monkeypatch.setenv('LLM_HTTP_BACKEND', 'sdk')
    return runtime


@pytest.fixture
def http_mock(monkeypatch, environment):
    with DefaultHttpxClient() as probe:
        name = next(t.__module__.split('.')[0] for t in type(probe).__mro__
                    if t.__module__.split('.')[0] in {'httpx', 'httpx2'})
    http = import_module(name)
    requests, clients = [], []
    failures = []

    def handler(request):
        requests.append(request)
        if failures:
            status = failures.pop(0)
            return http.Response(status, json={'error': {'message': 'controlled failure'}})
        return http.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
            'model': 'model-A', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'},
                                         'finish_reason': 'stop'}]})

    def factory(**kwargs):
        client = DefaultHttpxClient(transport=http.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr('agent.llm_runtime.DefaultHttpxClient', factory)
    return requests, clients, failures


def invoke(snapshot):
    with analysis_llm(runtime_snapshot=snapshot) as model:
        return model.invoke('OK').content


def switch_defaults(monkeypatch):
    monkeypatch.setenv('LLM_MODEL', 'model-B')
    monkeypatch.setenv('LLM_BASE_URL', 'https://b.example/other')
    monkeypatch.setenv('LLM_ENABLE_THINKING', 'true')
    monkeypatch.setenv('LLM_MAX_OUTPUT_TOKENS', '999')


def test_concurrent_clients_use_actual_pinned_requests(environment, http_mock, monkeypatch):
    requests, clients, _ = http_mock
    monkeypatch.setenv('LLM_ENABLE_THINKING', 'false')
    snapshot_a = capture_runtime(read_timeout=7, retries=0, trust_env=False, max_output_tokens=123)
    switch_defaults(monkeypatch)
    snapshot_b = capture_runtime(read_timeout=19, retries=0, trust_env=False)
    # 改成不可用的全局 Provider/Backend，显式快照也不应再解析它们。
    monkeypatch.setenv('LLM_PROVIDER', 'invalid-default')
    monkeypatch.setenv('LLM_HTTP_BACKEND', 'invalid-default')
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(invoke, [snapshot_a, snapshot_b])) == ['OK', 'OK']
    by_model = {json.loads(request.content)['model']: request for request in requests}
    for name, host, timeout, limit, thinking in [('model-A', 'a.example', 7, 123, False),
                                               ('model-B', 'b.example', 19, 999, True)]:
        request = by_model[name]
        body = json.loads(request.content)
        assert request.url.host == host
        assert request.extensions['timeout']['read'] == timeout
        assert body['max_completion_tokens'] == limit
        assert body['enable_thinking'] is thinking
        assert request.headers['authorization'] == 'Bearer test-only-private-credential'
    assert all(client.is_closed for client in clients)


def test_sdk_retry_and_serialized_restart_keep_original_request(environment, http_mock, monkeypatch):
    requests, clients, failures = http_mock
    saved = capture_runtime(read_timeout=11, retries=1, trust_env=False).model_dump_json()
    switch_defaults(monkeypatch)
    failures.append(503)
    assert invoke(RuntimeSnapshot.model_validate_json(saved)) == 'OK'
    assert len(requests) == 2
    assert all(json.loads(r.content)['model'] == 'model-A' and r.url.host == 'a.example'
               and r.extensions['timeout']['read'] == 11 for r in requests)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize('change', ['credential', 'identity_key', 'proxy', 'certificate', 'organization'])
def test_unavailable_runtime_stops_before_any_request(environment, http_mock, monkeypatch, tmp_path, change):
    cert = tmp_path / 'cert.pem'
    cert.write_text('test certificate A')
    if change == 'certificate':
        monkeypatch.setenv('SSL_CERT_FILE', str(cert))
    snapshot = capture_runtime(read_timeout=5, trust_env=True)
    if change == 'credential':
        monkeypatch.setenv('LLM_API_KEY', 'a-different-private-credential')
    elif change == 'identity_key':
        environment.IDENTITY_KEY.unlink()
    elif change == 'proxy':
        monkeypatch.setenv('HTTPS_PROXY', 'http://user:private@proxy.example:8080')
    elif change == 'organization':
        monkeypatch.setenv('OPENAI_ORG_ID', 'org-new')
    else:
        cert.write_text('test certificate B')
    with pytest.raises(RuntimeUnavailable):
        invoke(snapshot)
    assert http_mock[0] == [] and http_mock[1] == []


def test_snapshot_contains_no_credentials_or_proxy_password(environment, monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://user:proxy-password@proxy.example:8080')
    snapshot = capture_runtime(read_timeout=6)
    encoded = snapshot.model_dump_json()
    assert 'test-only-private-credential' not in encoded and 'proxy-password' not in encoded
    assert 'env:LLM_API_KEY' in encoded
    assert environment.IDENTITY_KEY.stat().st_mode & 0o777 == 0o600
    assert snapshot.id == capture_runtime(read_timeout=6).id
    with pytest.raises(ValidationError):
        snapshot.model = 'changed'


@pytest.mark.parametrize('url', ['https://user:pass@example.com/v1', 'https://example.com/v1?token=private',
                               'https://example.com/v1#private'])
def test_credential_urls_are_rejected(environment, monkeypatch, url):
    monkeypatch.setenv('LLM_BASE_URL', url)
    with pytest.raises(ValueError):
        capture_runtime(read_timeout=1)


@pytest.mark.parametrize('payload', [{'password': 'x'}, {'url': 'postgresql://user:pass@host/db'},
                                   {'url': 'https://user@host/path'}, {'url': 'https://host/path?token=secret'},
                                   {'prompt': 'test-only-private-credential'}])
def test_known_secret_or_credential_field_cannot_be_persisted(environment, payload):
    with pytest.raises(ValueError, match='未保存'):
        safe_manifest(payload)


@pytest.fixture
def version(environment, tmp_path):
    root = tmp_path / 'code'
    root.mkdir()
    (root / 'main.py').write_text('print(1)\n')
    (root / '.gitignore').write_text('.env\noutputs/\n')
    def git(*args):
        subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
    git('init')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture')
    (root / 'main.py').write_text('print(2)\n')
    (root / '.env').write_text('private-secret-never-read')
    captured = AgentVersion.capture(role='analysis', contracts={'analysis': {
        'prompt': 'Use only provided evidence.', 'schema': {'type': 'object'}}},
        tools={'structured_output_method': 'json_schema', 'batch_retries': 1}, quote_protocol='literal-v1', root=root)
    return root, captured


def test_dirty_code_and_real_contracts_are_preserved(version):
    root, value = version
    code = json.loads(value.code_json)
    assert code['dirty'] is True and len(code['git_sha']) == 40
    assert '.env' not in code['files']
    value.assert_available(root=root)
    contract = value.contract('analysis')
    contract['prompt'] = 'external mutation'
    assert value.contract('analysis')['prompt'] == 'Use only provided evidence.'
    assert value.id == AgentVersion.model_validate_json(value.model_dump_json()).id


def test_code_or_dependency_change_rejects_recovery(version, monkeypatch):
    root, value = version
    monkeypatch.setattr('agent.agent_version.dependencies', lambda: {'python': 'changed'})
    with pytest.raises(RuntimeUnavailable, match='依赖'):
        value.assert_available(root=root)
    (root / 'main.py').write_text('print(3)\n')
    with pytest.raises(RuntimeUnavailable, match='代码'):
        value.assert_available(root=root)


def test_batch_retry_factory_ignores_changed_defaults_and_restores_from_registry(version, http_mock, monkeypatch, tmp_path):
    root, value = version
    snapshot = capture_runtime(read_timeout=13, trust_env=False)
    engine = create_engine('sqlite:///' + str(tmp_path / 'versions.sqlite3'))
    for table in TABLES:
        table.create(engine)
    try:
        with Session(engine) as session, session.begin():
            assert save_immutable(session, value) == save_immutable(session, value)
            assert save_immutable(session, snapshot) == save_immutable(session, snapshot)
        switch_defaults(monkeypatch)
        # 新 Session 模拟进程重启，仅按编号读取，不依赖内存中的旧对象。
        with Session(engine) as session:
            restored_version = load_immutable(session, value.id)
            restored_runtime = load_immutable(session, snapshot.id)
            assert session.scalar(select(func.count()).select_from(AgentVersionRecord)) == 1
            assert session.scalar(select(func.count()).select_from(RuntimeSnapshotRecord)) == 1
        factory = pinned_llm_factory(restored_runtime, version=restored_version, root=root)
        for _ in range(2):
            with factory(read_timeout=99, retries=3, trust_env=True) as llm:
                assert llm.invoke('OK').content == 'OK'
        assert len(http_mock[0]) == 2
        assert all(json.loads(r.content)['model'] == 'model-A' and r.extensions['timeout']['read'] == 13
                   for r in http_mock[0])
        (root / 'main.py').write_text('changed')
        with pytest.raises(RuntimeUnavailable):
            factory()
        assert len(http_mock[0]) == 2
    finally:
        engine.dispose()


def test_registry_respects_outer_rollback_and_detects_tampering(environment):
    engine = create_engine('sqlite://')
    for table in TABLES:
        table.create(engine)
    value = capture_runtime(read_timeout=10)
    try:
        with Session(engine) as session:
            save_immutable(session, value)
            session.rollback()
        with Session(engine) as session, session.begin():
            with pytest.raises(RuntimeUnavailable, match='不存在'):
                load_immutable(session, value.id)
            save_immutable(session, value)
        with Session(engine) as session, session.begin():
            changed = value.model_dump(mode='json') | {'model': 'tampered'}
            session.execute(update(RuntimeSnapshotRecord).where(RuntimeSnapshotRecord.id == value.id).values(payload=changed))
        with Session(engine) as session:
            with pytest.raises(RuntimeUnavailable, match='校验失败'):
                load_immutable(session, value.id)
            with pytest.raises(RuntimeUnavailable, match='拒绝覆盖'):
                save_immutable(session, value)
    finally:
        engine.dispose()
