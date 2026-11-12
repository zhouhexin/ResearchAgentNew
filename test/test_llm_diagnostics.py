from contextlib import contextmanager
import json
from types import SimpleNamespace
import sys

import pytest
from openai import APIConnectionError

import diagnose_llm


@pytest.fixture
def configured(monkeypatch):
    config = {'LLM_API_KEY': 'diagnostic-secret-key', 'LLM_MODEL': 'test-model',
              'LLM_BASE_URL': 'https://model.example.org/v1',
              'LLM_ENABLE_THINKING': 'false', 'LLM_MAX_OUTPUT_TOKENS': '4096'}
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(diagnose_llm, 'dotenv_values', lambda path: config)
    return config


def test_comparison_keeps_attempts_separate_and_redacts_exception_text(configured, monkeypatch):
    calls, output = [], []
    monkeypatch.setattr(diagnose_llm, 'tls_probe', lambda *args: {'ok': True, 'tls_version': 'TLSv1.3'})

    def sdk(timeout):
        calls.append('sdk')
        if calls.count('sdk') == 1:
            exc = APIConnectionError(request=None)
            exc.__cause__ = ConnectionResetError('diagnostic-secret-key private details')
            raise exc
        return {'ok': True, 'response_received': True}

    def http(timeout):
        calls.append('requests')
        return {'ok': True, 'response_received': True, 'http_status': 200}

    monkeypatch.setattr(diagnose_llm, 'sdk_probe', sdk)
    monkeypatch.setattr(diagnose_llm, 'http_probe', http)
    report = diagnose_llm.run_diagnostics(emit=output.append)
    assert calls == ['sdk', 'requests', 'sdk', 'requests']
    assert report['successes'] == {'SDK_': 1, 'REQUESTS_': 2}
    assert report['matches_env_file']['LLM_API_KEY'] is True
    assert 'ConnectionResetError' in report['results'][1]['diagnostic']['causes']
    serialized = json.dumps(report) + ''.join(output)
    assert 'diagnostic-secret-key' not in serialized and 'private details' not in serialized


@pytest.mark.parametrize('status', [200, 403, 502])
def test_http_probe_uses_configured_key_and_matching_small_payload(configured, monkeypatch, status):
    class Response:
        status_code = status
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def json(self):
            return {'choices': [{'message': {'content': 'diagnostic-secret-key'}}]}

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            assert self.trust_env is True
            assert url == 'https://model.example.org/v1/chat/completions'
            assert kwargs['headers']['Authorization'] == 'Bearer diagnostic-secret-key'
            assert kwargs['json']['enable_thinking'] is False
            assert kwargs['json']['max_completion_tokens'] == 32
            assert kwargs['timeout'] == (10, 15)
            return Response()

    monkeypatch.setattr(diagnose_llm.requests, 'Session', Session)
    report = diagnose_llm.http_probe(15)
    assert report['ok'] is (status == 200) and report['response_received'] is True
    assert report['http_status'] == status
    assert 'diagnostic-secret-key' not in json.dumps(report)


def test_sdk_probe_uses_existing_runtime_and_disables_retries(configured, monkeypatch):
    class Model:
        def bind(self, **kwargs):
            assert kwargs == {'max_tokens': 32}
            return self
        def invoke(self, messages):
            assert messages == [('human', diagnose_llm.PROMPT)]
            return SimpleNamespace(content='OK')

    @contextmanager
    def factory(**kwargs):
        assert kwargs == {'read_timeout': 15, 'retries': 0, 'trust_env': True, 'http_backend': 'sdk'}
        yield Model()

    monkeypatch.setattr(diagnose_llm, 'analysis_llm', factory)
    assert diagnose_llm.sdk_probe(15) == {'ok': True, 'response_received': True}


def test_command_writes_safe_report(configured, monkeypatch, tmp_path):
    monkeypatch.setattr(diagnose_llm, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['diagnose_llm.py', '--attempts', '1'])
    monkeypatch.setattr(diagnose_llm, 'tls_probe', lambda *args: {'ok': True})
    monkeypatch.setattr(diagnose_llm, 'sdk_probe', lambda *args: {'ok': True})
    monkeypatch.setattr(diagnose_llm, 'http_probe', lambda *args: {'ok': False, 'http_status': 502})
    diagnose_llm.main()
    text = (tmp_path / 'outputs' / 'llm_network_diagnostic.json').read_text()
    assert 'diagnostic-secret-key' not in text
    assert json.loads(text)['successes'] == {'SDK_': 1, 'REQUESTS_': 0}
