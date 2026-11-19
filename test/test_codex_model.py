"""用独立假 CLI 进程验证桥接协议、凭证隔离和失败行为；不访问真实账号。"""
import json
import os
from pathlib import Path
import sys
from time import monotonic

import pytest

from agent.analysis_validator import EvidenceValidationError
from agent.codex_model import CodexExecutionError, CodexModel, child_environment
from agent.codex_model import execution_error
from agent.llm_runtime import analysis_llm, model_endpoint, model_http_backend, model_name
from agent.model_check import check_structured_analysis
from agent.batch_analysis import retryable


def test_snapshot_pins_cli_model_binary_and_proxy_through_structured_clone(cli, monkeypatch, tmp_path):
    from agent.runtime_snapshot import capture_runtime, RuntimeUnavailable
    monkeypatch.setattr('agent.runtime_snapshot.IDENTITY_KEY', tmp_path / 'identity.key')
    monkeypatch.setenv('CODEX_MODEL', 'pinned-model')
    monkeypatch.setenv('CODEX_USE_SYSTEM_PROXY', 'false')
    snapshot = capture_runtime(read_timeout=10)
    monkeypatch.setenv('CODEX_MODEL', 'new-default-model')
    monkeypatch.setenv('CODEX_CLI_PATH', '/unavailable/new-codex')
    monkeypatch.setenv('CODEX_USE_SYSTEM_PROXY', 'true')
    monkeypatch.setenv('LLM_PROVIDER', 'openai')
    with analysis_llm(runtime_snapshot=snapshot) as model:
        assert check_structured_analysis(model, 'json_schema') == {'findings': 1, 'quotes': 1}
        record = json.loads(cli.read_text())
        assert record['args'][record['args'].index('--model') + 1] == 'pinned-model'
        assert 'features.respect_system_proxy=false' in record['args']
        # 同一客户端的下一次调用也必须检查原网络配置。
        monkeypatch.setenv('HTTPS_PROXY', 'http://new-proxy.example:8080')
        with pytest.raises(RuntimeUnavailable):
            model.invoke('OK')


def test_codex_snapshot_requires_explicit_model_and_original_binary(cli, monkeypatch, tmp_path):
    from agent.runtime_snapshot import capture_runtime, RuntimeUnavailable
    monkeypatch.setattr('agent.runtime_snapshot.IDENTITY_KEY', tmp_path / 'identity.key')
    with pytest.raises(ValueError, match='CODEX_MODEL'):
        capture_runtime(read_timeout=10)
    monkeypatch.setenv('CODEX_MODEL', 'pinned-model')
    snapshot = capture_runtime(read_timeout=10)
    Path(snapshot.codex_executable).write_text('changed executable')
    with pytest.raises(RuntimeUnavailable, match='执行文件'):
        with analysis_llm(runtime_snapshot=snapshot):
            pytest.fail('不得创建更换了执行文件的客户端')


@pytest.mark.parametrize('change', ['version', 'path', 'login_home'])
def test_codex_snapshot_refuses_changed_tool_or_login_environment(cli, monkeypatch, tmp_path, change):
    from agent.runtime_snapshot import capture_runtime, RuntimeUnavailable
    monkeypatch.setattr('agent.runtime_snapshot.IDENTITY_KEY', tmp_path / 'identity.key')
    monkeypatch.setenv('CODEX_MODEL', 'pinned-model')
    snapshot = capture_runtime(read_timeout=10)
    assert snapshot.codex_cli_version == 'fake-codex 1.0'
    if change == 'version':
        monkeypatch.setenv('FAKE_CODEX_VERSION', 'fake-codex 2.0')
    elif change == 'path':
        monkeypatch.setenv('PATH', str(tmp_path))
    else:
        monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'other-login'))
    with pytest.raises(RuntimeUnavailable):
        with analysis_llm(runtime_snapshot=snapshot):
            pytest.fail('不得恢复已改变的 CLI 环境')
    assert not cli.exists()


@pytest.fixture
def cli(monkeypatch, tmp_path):
    binary = tmp_path / 'fake-codex'
    capture = tmp_path / 'capture.json'
    binary.write_text(f'#!{sys.executable}\n' + r'''
import json, os, pathlib, sys, time
args = sys.argv[1:]
mode = os.getenv('FAKE_CODEX_MODE', 'valid')
if args == ['--version']:
    print(os.getenv('FAKE_CODEX_VERSION', 'fake-codex 1.0'))
    sys.exit(0)
if args == ['login', 'status']:
    print('Logged in using API key' if mode == 'api_login' else 'Logged in using ChatGPT')
    sys.exit(0)
prompt = sys.stdin.read()
schema = json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text()) if '--output-schema' in args else None
pathlib.Path(os.environ['FAKE_CODEX_CAPTURE']).write_text(json.dumps({
    'args': args, 'prompt': prompt, 'schema': schema, 'cwd': os.getcwd(),
    'env_keys': sorted(os.environ), 'codex_home': os.getenv('CODEX_HOME')
}))
if mode == 'timeout':
    time.sleep(30)
if mode == 'json_network':
    print('WARNING: proceeding, Operation not permitted', file=sys.stderr)
    print(json.dumps({'type': 'error', 'message': 'dns error sending request to https://user:private-upstream-secret@chatgpt.com/backend-api/codex/responses?token=private-upstream-secret'}))
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'failed to complete turn'}}))
    pathlib.Path(args[args.index('-o')+1]).write_text('partial response must not be accepted')
    sys.exit(0)
if mode in {'quota', 'auth', 'network', 'permission'}:
    print({'quota': 'usage_limit_reached', 'auth': 'authentication token expired',
           'network': 'error sending request connection reset', 'permission': 'Operation not permitted'}[mode] + ' private-upstream-secret', file=sys.stderr)
    sys.exit(1)
if mode == 'empty':
    sys.exit(0)
value = 'OK'
if schema:
    if schema['properties'].get('translation_zh'):
        value = json.dumps({'translation_zh': '我们评估深度估计。准确率提高了2.5%。'})
    elif schema['properties'].get('steps'):
        value = pathlib.Path(os.environ['FAKE_CODEX_PLAN_PATH']).read_text()
    elif schema['properties'].get('papers'):
        payload = json.loads(json.loads(prompt[prompt.index('\n')+1:])[1]['content'])
        value = json.dumps({'papers': [{'candidate_id': p['candidate_id'], 'checks': [
          {'criterion_id': c['id'], 'verdict': 'unknown', 'quote': '', 'explanation': '元数据不足，需进一步确认。'}
          for c in payload['criteria']]} for p in payload['papers']]})
    elif schema['properties'].get('questions'):
        payload = json.loads(json.loads(prompt[prompt.index('\n')+1:])[1]['content'])
        refs = [finding['evidence'][0]['ref'] for paper in payload['papers']
                for findings in paper['findings'].values() for finding in findings]
        value = json.dumps({'questions': [{'question': '所选论文的方法和实验有何差异？',
          'rationale': '论文分别提供了带证据的研究发现，可比较这些维度。', 'evidence_refs': refs}]})
    elif schema['properties'].get('research_questions'):
        payload = json.loads(prompt[prompt.index('\n')+1:])[1]['content']
        evidence = json.loads(payload)['evidence'][0]
        value = json.dumps({'research_questions': [{'statement': '样例研究问题', 'evidence': [{
            'evidence_id': evidence['evidence_id'], 'quote': 'fabricated quote' if mode == 'bad_quote' else evidence['text']
        }]}], 'methodologies': [], 'contributions': [], 'experiments': [], 'limitations': []})
    else:
        refs = ['P1:E1', 'P2:E2']
        try:
            payload = json.loads(json.loads(prompt[prompt.index('\n')+1:])[1]['content'])
            refs = [finding['evidence'][0]['ref'] for paper in payload['papers']
                    for findings in paper['findings'].values() for finding in findings]
        except (ValueError, KeyError, IndexError):
            pass
        value = json.dumps({'title': '多篇比较', 'background': [], 'common_methods': [],
          'comparisons': [{'claim': '方法比较', 'evidence_refs': refs}],
          'limitations': [], 'tentative_directions': []})
if mode == 'bad_json':
    value = 'not json'
pathlib.Path(args[args.index('-o')+1]).write_text(value)
if mode == 'recovered':
    print(json.dumps({'type': 'error', 'message': 'temporary connection reset'}))
    print(json.dumps({'type': 'turn.completed'}))
print('stdout is progress noise, not the final response')
''')
    binary.chmod(0o700)
    monkeypatch.setenv('LLM_PROVIDER', 'codex')
    monkeypatch.setenv('CODEX_CLI_PATH', str(binary))
    monkeypatch.setenv('FAKE_CODEX_CAPTURE', str(capture))
    monkeypatch.delenv('CODEX_MODEL', raising=False)
    return capture


@pytest.mark.parametrize('method', ['json_schema', 'function_calling'])
def test_cli_uses_saved_chatgpt_login_and_real_evidence_validation(cli, monkeypatch, method):
    for key in ('LLM_API_KEY', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'DATABASE_URL'):
        monkeypatch.setenv(key, 'private-secret')
    monkeypatch.setenv('CODEX_HOME', '/existing/auth/home')
    with analysis_llm(read_timeout=10, retries=0, trust_env=True) as model:
        assert check_structured_analysis(model, method) == {'findings': 1, 'quotes': 1}
    record = json.loads(cli.read_text())
    assert 'private-secret' not in cli.read_text()
    assert not {'LLM_API_KEY', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'DATABASE_URL'} & set(record['env_keys'])
    assert record['codex_home'] == '/existing/auth/home'
    assert record['args'][-1] == '-' and '--ephemeral' in record['args']
    assert '--ignore-user-config' in record['args']
    assert 'forced_login_method="chatgpt"' in record['args']
    assert 'features.shell_tool=false' in record['args'] and 'web_search="disabled"' in record['args']
    assert '--json' in record['args'] and 'features.respect_system_proxy=true' in record['args']
    assert record['schema']['additionalProperties'] is False
    assert not Path(record['cwd']).exists()  # 原文、schema 和结果临时文件已经清除。


def test_plain_response_reads_final_file_and_does_not_require_api_config(cli, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    monkeypatch.setenv('CODEX_MODEL', 'account-available-model')
    monkeypatch.setenv('LLM_ENABLE_THINKING', 'not-an-api-option')
    with analysis_llm(read_timeout=10, retries=0, trust_env=True) as model:
        assert model.invoke('只回复 OK').content == 'OK'
    assert 'account-available-model' in json.loads(cli.read_text())['args']
    assert model_name() == 'account-available-model'
    assert model_http_backend() == 'codex' and model_endpoint() == 'codex://chatgpt'
    with pytest.raises(ValueError, match='Codex 模式'):
        model_http_backend('requests')


def test_wrong_quotes_are_still_rejected(cli, monkeypatch):
    monkeypatch.setenv('FAKE_CODEX_MODE', 'bad_quote')
    with pytest.raises(EvidenceValidationError):
        check_structured_analysis(CodexModel(timeout=10), 'json_schema')


def test_invalid_json_is_structural_failure(cli, monkeypatch):
    from agent.paper_analyzer import StructuredAnalysisError
    monkeypatch.setenv('FAKE_CODEX_MODE', 'bad_json')
    with pytest.raises(StructuredAnalysisError):
        check_structured_analysis(CodexModel(timeout=10), 'json_schema')


@pytest.mark.parametrize('mode,code,can_retry', [
    ('quota', 'MODEL_QUOTA_EXCEEDED', False), ('auth', 'CODEX_AUTH_REQUIRED', False),
    ('api_login', 'CODEX_AUTH_REQUIRED', False), ('network', 'MODEL_CONNECTION_ERROR', True),
    ('permission', 'CODEX_PERMISSION_DENIED', False),
    ('empty', 'CODEX_EMPTY_RESPONSE', True),
])
def test_safe_failures_never_fall_back_to_api_key(cli, monkeypatch, mode, code, can_retry):
    monkeypatch.setenv('FAKE_CODEX_MODE', mode)
    with pytest.raises(CodexExecutionError) as error:
        CodexModel(timeout=10).invoke('OK')
    assert error.value.code == code and retryable(error.value) is can_retry
    assert 'private-upstream-secret' not in str(error.value) + json.dumps(error.value.diagnostic)
    if mode == 'api_login':
        assert not cli.exists()


def test_deadline_kills_cli_process_group(cli, monkeypatch):
    import agent.codex_model as bridge
    monkeypatch.setenv('FAKE_CODEX_MODE', 'timeout')
    # 登录探测本身已在其他用例走真实子进程；本用例确保时间预算用于执行进程。
    monkeypatch.setattr(bridge.subprocess, 'run', lambda *a, **kw:
                        bridge.subprocess.CompletedProcess(a[0], 0, 'Logged in using ChatGPT', ''))
    original, killed = os.killpg, []
    def kill(pid, sig):
        killed.append(pid)
        original(pid, sig)
    monkeypatch.setattr(bridge.os, 'killpg', kill)
    started = monotonic()
    with pytest.raises(CodexExecutionError) as error:
        CodexModel(timeout=0.6).invoke('OK')
    assert error.value.code == 'MODEL_TIMEOUT' and len(killed) == 1
    assert monotonic() - started < 5


def test_no_proxy_removes_proxy_and_certificate_overrides(monkeypatch):
    for name in ('HTTP_PROXY', 'https_proxy', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'):
        monkeypatch.setenv(name, 'private-value')
    assert all(name not in child_environment(False) for name in ('HTTP_PROXY', 'https_proxy', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'))


def test_review_uses_same_schema_bridge(cli):
    from review.synthesis import ReviewDraft, parse_review_response, validate_review
    response = CodexModel(timeout=10).with_structured_output(ReviewDraft, include_raw=True).invoke([('human', '生成综述')])
    draft = parse_review_response(response, 'json_schema')
    validate_review(draft, {'P1:E1', 'P2:E2'})
    assert draft.title == '多篇比较'


@pytest.mark.parametrize('text,reason,phase,code', [
    ('dns error: failed to lookup address', 'dns', 'connect', 'MODEL_CONNECTION_ERROR'),
    ('TLS certificate unknown issuer', 'tls', 'tls', 'MODEL_CONNECTION_ERROR'),
    ('HTTP status 407 proxy authentication required', 'proxy', 'connect', 'MODEL_CONNECTION_ERROR'),
    ('unexpected status 403 Forbidden', 'http', 'response', 'MODEL_HTTP_ERROR'),
    ('unexpected status 502 Bad Gateway', 'http', 'response', 'MODEL_HTTP_ERROR'),
    ('WebSocket stream disconnected', 'connection', 'websocket', 'MODEL_CONNECTION_ERROR'),
    ('connection reset', 'interrupted', 'read', 'MODEL_CONNECTION_ERROR'),
    ('unexpected status 400: The model GPT-6-Luna is not supported with this account', 'model', 'codex', 'CODEX_MODEL_UNAVAILABLE'),
    ('unexpected status 401 Unauthorized', 'auth', 'codex', 'CODEX_AUTH_REQUIRED'),
    ('unexpected status 429', 'quota', 'codex', 'MODEL_QUOTA_EXCEEDED'),
])
def test_diagnostics_separate_model_auth_http_and_network_errors(text, reason, phase, code):
    error = execution_error(text, exit_code=1)
    assert error.code == code
    assert error.diagnostic['reason'] == reason and error.diagnostic['phase'] == phase
    assert error.diagnostic['cli_exit_code'] == 1


def test_json_failure_prevents_partial_success_and_saves_only_safe_details(cli, monkeypatch):
    monkeypatch.setenv('FAKE_CODEX_MODE', 'json_network')
    monkeypatch.setenv('HTTPS_PROXY', 'http://user:private-proxy-secret@localhost:1234')
    with pytest.raises(CodexExecutionError) as result:
        CodexModel(timeout=10).invoke('OK')
    error = result.value
    assert error.diagnostic['reason'] == 'dns' and error.diagnostic['host'] == 'chatgpt.com'
    assert error.diagnostic['cli_exit_code'] == 0
    assert error.diagnostic['system_proxy_enabled'] is True
    assert 'HTTPS_PROXY' in error.diagnostic['environment_proxy_names']
    safe = str(error) + json.dumps(error.diagnostic)
    assert all(value not in safe for value in ('private-upstream-secret', 'private-proxy-secret', 'user:', '/backend-api', 'token='))


def test_recovered_error_event_does_not_discard_final_success(cli, monkeypatch):
    monkeypatch.setenv('FAKE_CODEX_MODE', 'recovered')
    assert CodexModel(timeout=10).invoke('OK').content == 'OK'


def test_no_proxy_disables_system_proxy_in_cli_too(cli):
    assert CodexModel(timeout=10, trust_env=False).invoke('OK').content == 'OK'
    assert 'features.respect_system_proxy=false' in json.loads(cli.read_text())['args']


def test_official_codex_model_ids_are_case_normalized(cli, monkeypatch):
    monkeypatch.setenv('CODEX_MODEL', 'GPT-6-Luna')
    with analysis_llm(read_timeout=10, retries=0, trust_env=True) as model:
        assert model.model == 'gpt-6-luna'
    assert model_name() == 'gpt-6-luna'
