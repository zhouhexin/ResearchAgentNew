from dataclasses import replace
from importlib import import_module
from contextlib import contextmanager
from pathlib import Path
import runpy
import sys

import pytest
from openai import APITimeoutError, DefaultHttpxClient

from agent.analysis_validator import EvidenceSource
from agent.llm_runtime import analysis_llm
from agent.paper_context import limit_context_chars
from test.test_paper_analysis import make_context


def test_character_budget_keeps_complete_evidence():
    context = make_context()
    context = replace(context, total_chunks=20, sources={
        f"E{i}": EvidenceSource(f"E{i}", str(i) * 100, context.source_url, i, None)
        for i in range(1, 7)
    })
    limited = limit_context_chars(context, 350)
    assert len(limited.sources) == 3
    assert list(limited.sources) == ["E1", "E3", "E6"]
    assert limited.total_chunks == 20
    assert context.sources.keys() == {f"E{i}" for i in range(1, 7)}
    assert all(source is context.sources[key] for key, source in limited.sources.items())
    assert sum(len(s.text) for s in limited.sources.values()) <= 350


def test_budget_too_small_does_not_truncate_quote():
    with pytest.raises(ValueError, match="完整放入"):
        limit_context_chars(make_context(), 1)


def mock_client(monkeypatch, handler):
    # 选择当前 SDK 使用的 HTTP 库，兼容 httpx 与 httpx2。
    with DefaultHttpxClient() as probe:
        name = next(t.__module__.split(".")[0] for t in type(probe).__mro__
                    if t.__module__.split(".")[0] in {"httpx", "httpx2"})
    http = import_module(name)
    clients = []

    def factory(**kwargs):
        client = DefaultHttpxClient(transport=http.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("agent.llm_runtime.DefaultHttpxClient", factory)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.org/v1")
    return http, clients


def test_timeout_not_retried_and_client_is_closed(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        raise http.ReadTimeout("simulated timeout", request=request)

    http, clients = mock_client(monkeypatch, handler)
    with pytest.raises(APITimeoutError):
        with analysis_llm(read_timeout=7, retries=0, trust_env=False) as llm:
            llm.invoke("OK")
    assert len(requests) == 1
    assert requests[0].extensions["timeout"]["read"] == 7
    assert clients[0].is_closed


def test_review_output_override_does_not_change_paper_analysis_budget(monkeypatch):
    import json
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return http.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'test-model',
                                      'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]})
    http, clients = mock_client(monkeypatch, handler)
    monkeypatch.setenv('LLM_MAX_OUTPUT_TOKENS', '4096')
    with analysis_llm(read_timeout=10, retries=0, trust_env=False, max_output_tokens=10000) as llm:
        llm.invoke('详细综述')
    with analysis_llm(read_timeout=10, retries=0, trust_env=False) as llm:
        llm.invoke('普通分析')
    assert [item['max_completion_tokens'] for item in requests] == [10000, 4096]
    assert all(client.is_closed for client in clients)


def test_tcp_peer_and_tls_failure_stage_survive_sdk_exception_wrapping(monkeypatch):
    from openai import APIConnectionError
    from agent.model_diagnostics import model_connection_diagnostic

    class Stream:
        def get_extra_info(self, name):
            return ('192.0.2.10', 443) if name == 'server_addr' else None

    def handler(request):
        trace = request.extensions['trace']
        trace('connection.connect_tcp.complete', {'return_value': Stream()})
        trace('connection.start_tls.started', {})
        trace('connection.start_tls.failed', {})
        raise http.ConnectError('private details', request=request) from ConnectionResetError(54, 'reset')

    http, clients = mock_client(monkeypatch, handler)
    with pytest.raises(APIConnectionError) as error:
        with analysis_llm(read_timeout=120, retries=0, trust_env=False) as llm:
            llm.invoke('OK')
    diagnostic = model_connection_diagnostic(error.value)
    assert diagnostic['network'] == {'backend': 'sdk', 'stage': 'connection.start_tls.failed',
                                     'peer_ip': '192.0.2.10', 'peer_port': 443}
    assert clients[0].is_closed


def test_success_uses_chat_endpoint_and_configured_timeout(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return http.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop"}],
        })

    http, clients = mock_client(monkeypatch, handler)
    # 无代理选项也覆盖 OPENAI_PROXY，避免外部代理与自定义客户端冲突。
    monkeypatch.setenv("OPENAI_PROXY", "http://127.0.0.1:9")
    with analysis_llm(read_timeout=400, retries=0, trust_env=False) as llm:
        assert llm.invoke("OK").content == "OK"
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].extensions["timeout"] == {
        "connect": 10.0, "read": 400, "write": 30.0, "pool": 10.0,
    }
    assert clients[0].is_closed


@pytest.mark.parametrize('method', ['function_calling', 'json_schema'])
def test_structured_check_sends_thinking_and_output_limits_without_database(monkeypatch, tmp_path, capsys, method):
    import json
    requests = []
    analysis = {
        'research_questions': [], 'methodologies': [{
            'statement': '通过检索原文片段回答问题',
            'evidence': [{'evidence_id': 'E1', 'quote': 'Our method retrieves two passages for each question'}],
        }], 'contributions': [], 'experiments': [], 'limitations': [],
    }

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        message = {'role': 'assistant', 'content': json.dumps(analysis)}
        if method == 'function_calling':
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'call-test', 'type': 'function',
                'function': {'name': 'PaperAnalysis', 'arguments': json.dumps(analysis)},
            }]}
        return http.Response(200, json={
            'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 0, 'model': 'test-model',
            'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}],
        })

    http, clients = mock_client(monkeypatch, handler)
    monkeypatch.setenv('LLM_ENABLE_THINKING', 'false')
    monkeypatch.setenv('LLM_MAX_OUTPUT_TOKENS', '4096')
    monkeypatch.setenv('STRUCTURED_OUTPUT_METHOD', method)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    script = tmp_path / 'run_paper_analysis.py'
    script.write_text((Path(__file__).resolve().parents[1] / script.name).read_text())
    monkeypatch.setattr(sys, 'argv', [str(script), '--check-llm', '--structured', '--timeout', '120'])
    runpy.run_path(str(script), run_name='__main__')
    assert len(requests) == 1
    assert requests[0]['enable_thinking'] is False
    assert requests[0]['max_completion_tokens'] == 4096
    assert 'extra_body' not in requests[0]
    assert ('tools' in requests[0]) == (method == 'function_calling')
    assert clients[0].is_closed
    assert not (tmp_path / 'outputs').exists()
    assert '结构化分析及原文引用核验通过' in capsys.readouterr().out


@pytest.mark.parametrize('key,value', [
    ('LLM_ENABLE_THINKING', 'no'), ('LLM_MAX_OUTPUT_TOKENS', '0'),
    ('LLM_MAX_OUTPUT_TOKENS', 'invalid'),
])
def test_invalid_generation_options_rejected(monkeypatch, key, value):
    from agent.llm_runtime import model_request_options
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        model_request_options()


def test_cli_timeout_preserves_existing_report(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[1] / "run_paper_analysis.py"
    copy = tmp_path / script.name
    copy.write_text(script.read_text())
    output = tmp_path / "outputs" / "paper_analysis_7_3.json"
    output.parent.mkdir()
    output.write_text("previous successful report")
    monkeypatch.setattr(sys, "argv", [str(copy), "7", "--batch-retries", "0"])
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("DATABASE_URL", "unused")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr('storage.pdf_reparse.ensure_pdf_reading_order', lambda *args, **kwargs: None)
    monkeypatch.setattr("agent.paper_context.load_paper_context", lambda *a, **kw: make_context())

    class Engine:
        def dispose(self):
            pass
    class Session:
        @staticmethod
        @contextmanager
        def begin():
            yield None
    monkeypatch.setattr("storage.database.make_db", lambda url: (Engine(), Session))
    monkeypatch.setattr("agent.paper_context.verify_paper_context", lambda *args: None)

    class TimeoutModel:
        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, messages):
            raise APITimeoutError(request=None)

    @contextmanager
    def fake_llm(**kwargs):
        yield TimeoutModel()

    monkeypatch.setattr("agent.llm_runtime.analysis_llm", fake_llm)
    with pytest.raises(SystemExit, match="模型请求超时") as error:
        runpy.run_path(str(copy), run_name="__main__")
    assert error.value.code != 0
    assert output.read_text() == "previous successful report"
    assert {p.name for p in output.parent.iterdir()} == {output.name, "api_tasks.sqlite3"}
    from storage.analysis_batches import AnalysisBatchStore
    import sqlite3
    with sqlite3.connect(output.parent / "api_tasks.sqlite3") as db:
        key = db.execute("SELECT run_key FROM analysis_batch_runs").fetchone()[0]
    assert AnalysisBatchStore(output.parent / "api_tasks.sqlite3").steps(key)[0]["status"] == "failed"


def test_cli_resumes_only_failed_batch(monkeypatch, tmp_path):
    import json
    script = Path(__file__).resolve().parents[1] / 'run_paper_analysis.py'
    copy = tmp_path / script.name
    copy.write_text(script.read_text())
    context = make_context()
    context = replace(context, total_chunks=2, sources={**context.sources,
        'E12': EvidenceSource('E12', 'Another original paragraph.', context.source_url, 3, None)})
    monkeypatch.setattr(sys, 'argv', [str(copy), '7', '--batch-size', '1', '--batch-retries', '0'])
    monkeypatch.setenv('DATABASE_URL', 'unused')
    prepared = []
    def prepare_pdf(url, paper_id, **kwargs):
        prepared.append(paper_id)
    monkeypatch.setattr('storage.pdf_reparse.ensure_pdf_reading_order', prepare_pdf)
    monkeypatch.setenv('LLM_MODEL', 'test-model')
    monkeypatch.setattr('agent.paper_context.load_paper_context', lambda *a, **kw: context)
    monkeypatch.setenv('LLM_API_KEY', 'test-key')
    monkeypatch.setattr('agent.paper_context.verify_paper_context', lambda *a: None)
    monkeypatch.setattr('agent.paper_context.assert_document_unchanged', lambda *a: None)
    class Engine:
        def dispose(self):
            pass
    class Session:
        @staticmethod
        @contextmanager
        def begin():
            yield None
    monkeypatch.setattr('storage.database.make_db', lambda url: (Engine(), Session))
    calls, state = [], {'fail': True}
    class Model:
        def with_structured_output(self, *a, **kw):
            return self
        def invoke(self, messages):
            item = json.loads(messages[1][1])['evidence'][0]
            calls.append(item['evidence_id'])
            if state['fail'] and item['evidence_id'] == 'E12':
                raise APITimeoutError(request=None)
            return {'research_questions': [{'statement': item['evidence_id'], 'evidence': [{'evidence_id': item['evidence_id'], 'quote': item['text']}]}],
                    'methodologies': [], 'contributions': [], 'experiments': [], 'limitations': []}
    @contextmanager
    def factory(**kw):
        yield Model()
    monkeypatch.setattr('agent.llm_runtime.analysis_llm', factory)
    with pytest.raises(SystemExit, match='模型请求超时'):
        runpy.run_path(str(copy), run_name='__main__')
    state['fail'] = False
    runpy.run_path(str(copy), run_name='__main__')
    assert calls == ['E11', 'E12', 'E12']
    assert prepared == [7, 7]
    report = json.loads((tmp_path / 'outputs' / 'paper_analysis_7_3.json').read_text())
    assert report['batch_summary']['resumed_batches'] == 1
    assert len(report['analysis']['research_questions']) == 2
