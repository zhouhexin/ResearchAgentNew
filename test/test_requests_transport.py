import json
from importlib import import_module

from openai import APIConnectionError, APITimeoutError, APIStatusError, DefaultHttpxClient
import pytest
import requests

from agent.llm_runtime import analysis_llm
from agent.model_diagnostics import model_connection_diagnostic
from agent.paper_analyzer import analyze_paper
from test.test_paper_analysis import make_context, make_analysis


@pytest.fixture
def transport(monkeypatch):
    for key, value in {'LLM_API_KEY': 'test-secret-key', 'LLM_MODEL': 'test-model',
                       'LLM_BASE_URL': 'https://example.org/v1', 'LLM_ENABLE_THINKING': 'false',
                       'LLM_MAX_OUTPUT_TOKENS': '4096'}.items():
        monkeypatch.setenv(key, value)
    state = {'calls': [], 'sessions': [], 'status': 200, 'error': None, 'response': None}

    class Response:
        headers = {'Content-Type': 'application/json', 'Content-Encoding': 'gzip', 'Content-Length': '1'}
        def __init__(self):
            self.status_code = state['status']
        def __enter__(self): return self
        def __exit__(self, *args): pass
        @property
        def content(self):
            return json.dumps(state['response']).encode()

    class Session:
        def __init__(self):
            self.closed = False
            state['sessions'].append(self)
        def request(self, method, url, **kwargs):
            state['calls'].append((method, url, kwargs))
            if state['error']:
                raise state['error']
            return Response()
        def close(self): self.closed = True

    monkeypatch.setattr(requests, 'Session', Session)
    return state


@pytest.mark.parametrize('method', ['function_calling', 'json_schema'])
def test_requests_backend_runs_same_structured_analysis_and_evidence_validation(transport, method, monkeypatch):
    payload = make_analysis().model_dump_json()
    message = {'role': 'assistant', 'content': payload}
    if method == 'function_calling':
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'call-test', 'type': 'function', 'function': {'name': 'PaperAnalysis', 'arguments': payload}}]}
    transport['response'] = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 0,
                             'model': 'test-model', 'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}]}
    http = import_module(next(cls.__module__.split('.')[0] for cls in DefaultHttpxClient.__mro__
                              if cls.__module__.split('.')[0] in {'httpx', 'httpx2'}))
    original_payloads = []

    def sdk_handler(request):
        original_payloads.append(json.loads(request.content))
        return http.Response(200, json=transport['response'])

    def client_factory(**kwargs):
        if 'transport' not in kwargs:
            kwargs['transport'] = http.MockTransport(sdk_handler)
        return DefaultHttpxClient(**kwargs)

    monkeypatch.setattr('agent.llm_runtime.DefaultHttpxClient', client_factory)
    with analysis_llm(read_timeout=120, retries=0, trust_env=False, http_backend='sdk') as llm:
        assert analyze_paper(llm, make_context(), method=method, max_repair_attempts=0) == make_analysis()
    with analysis_llm(read_timeout=120, retries=0, trust_env=False, http_backend='requests') as llm:
        result = analyze_paper(llm, make_context(), method=method, max_repair_attempts=0)
    assert result == make_analysis()
    assert len(transport['calls']) == 1
    verb, url, kwargs = transport['calls'][0]
    body = json.loads(kwargs['data'])
    assert verb == 'POST' and url == 'https://example.org/v1/chat/completions'
    assert kwargs['headers']['authorization'] == 'Bearer test-secret-key'
    assert kwargs['timeout'] == (10, 120)
    assert kwargs['allow_redirects'] is False and kwargs['stream'] is True
    assert body['enable_thinking'] is False and body['max_completion_tokens'] == 4096
    assert ('tools' in body) == (method == 'function_calling')
    assert body == original_payloads[0]  # 完整请求体相同，不只比较模型或工具名。
    assert all(session.closed and not session.trust_env for session in transport['sessions'])


@pytest.mark.parametrize('error_type,expected', [
    (requests.ConnectTimeout, APITimeoutError), (requests.ReadTimeout, APITimeoutError),
    (requests.ConnectionError, APIConnectionError),
])
def test_requests_backend_preserves_network_errors_and_closes_client(transport, error_type, expected):
    transport['error'] = error_type('test-secret-key private details')
    transport['error'].__cause__ = ConnectionResetError(54, 'reset')
    with pytest.raises(expected) as error:
        with analysis_llm(read_timeout=120, retries=0, trust_env=True, http_backend='requests') as llm:
            llm.invoke('OK')
    diagnostic = model_connection_diagnostic(error.value)
    assert diagnostic['network']['backend'] == 'requests'
    assert 'test-secret-key' not in json.dumps(diagnostic)
    assert len(transport['calls']) == 1
    assert all(session.closed for session in transport['sessions'])


def test_requests_backend_preserves_http_status_for_sdk_classification(transport):
    transport['status'] = 429
    transport['response'] = {'error': {'message': 'rate limited', 'type': 'rate_limit', 'code': 'rate_limit'}}
    with pytest.raises(APIStatusError) as error:
        with analysis_llm(read_timeout=120, retries=0, trust_env=True, http_backend='requests') as llm:
            llm.invoke('OK')
    assert error.value.status_code == 429 and len(transport['calls']) == 1
    assert all(session.closed for session in transport['sessions'])


def test_requests_backend_does_not_accept_fabricated_quotes(transport):
    from agent.analysis_validator import EvidenceValidationError
    transport['response'] = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 0,
                             'model': 'test-model', 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                                 'role': 'assistant', 'content': None, 'tool_calls': [{
                                     'id': 'call-test', 'type': 'function', 'function': {
                                         'name': 'PaperAnalysis', 'arguments': make_analysis(quote='fabricated quote').model_dump_json()}}]}}]}
    with pytest.raises(EvidenceValidationError):
        with analysis_llm(read_timeout=120, retries=0, trust_env=False, http_backend='requests') as llm:
            analyze_paper(llm, make_context(), method='function_calling', max_repair_attempts=0)
