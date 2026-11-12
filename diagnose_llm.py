"""对照 TLS、SDK 和 requests；不输出密钥、请求头、响应正文或异常原文。"""
import argparse
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import socket
import ssl
import sys
from time import monotonic
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv
import requests

from agent.llm_runtime import analysis_llm, model_request_options, model_provider
from agent.model_diagnostics import model_connection_diagnostic

ROOT = Path(__file__).resolve().parent
PROMPT = '请只回复 OK。'


def tls_probe(host, port, timeout):
    with socket.create_connection((host, port), timeout=min(10, timeout)) as connection:
        with ssl.create_default_context().wrap_socket(connection, server_hostname=host) as secure:
            return {'ok': True, 'tls_version': secure.version()}


def sdk_probe(timeout):
    with analysis_llm(read_timeout=timeout, retries=0, trust_env=True, http_backend='sdk') as llm:
        response = llm.bind(max_tokens=32).invoke([('human', PROMPT)])
    return {'ok': bool(response.content), 'response_received': True}


def http_probe(timeout):
    options = model_request_options()
    payload = {'model': os.environ['LLM_MODEL'], 'messages': [{'role': 'user', 'content': PROMPT}],
               'max_completion_tokens': 32, **options.get('extra_body', {})}
    endpoint = os.environ['LLM_BASE_URL'].rstrip('/') + '/chat/completions'
    with requests.Session() as session:
        # 与 SDK 一样读取当前进程的代理环境和证书配置，不绕过校验。
        session.trust_env = True
        with session.post(endpoint, headers={'Authorization': 'Bearer ' + os.environ['LLM_API_KEY']},
                          json=payload, timeout=(min(10, timeout), timeout)) as response:
            if response.status_code >= 400:
                return {'ok': False, 'response_received': True, 'http_status': response.status_code}
            try:
                body = response.json()
            except ValueError:
                return {'ok': False, 'response_received': True, 'http_status': response.status_code,
                        'error': 'INVALID_JSON_RESPONSE'}
            if not isinstance(body, dict):
                return {'ok': False, 'response_received': True, 'http_status': response.status_code,
                        'error': 'INVALID_MODEL_RESPONSE'}
            choices = body.get('choices', [])
            content = choices[0].get('message', {}).get('content') if choices else None
            return {'ok': bool(content), 'response_received': True, 'http_status': response.status_code}


def run_diagnostics(*, attempts=2, timeout=15, emit=print):
    if model_provider() == 'codex':
        raise ValueError('当前为 Codex 模式，此脚本仅对照 API 网络。请运行 python run_paper_analysis.py --check-llm --structured --timeout 120。')
    for name in ('LLM_MODEL', 'LLM_API_KEY', 'LLM_BASE_URL'):
        if not os.getenv(name):
            raise ValueError(f'请配置 {name}')
    target = urlsplit(os.environ['LLM_BASE_URL'])
    if target.scheme != 'https' or not target.hostname:
        raise ValueError('此网络诊断要求 LLM_BASE_URL 为有效 HTTPS 地址')
    file_config = dotenv_values(ROOT / '.env')
    libraries = {}
    for name in ('openai', 'langchain-openai', 'httpx', 'httpx2', 'requests'):
        try:
            libraries[name] = version(name)
        except PackageNotFoundError:
            pass
    report = {
        'python': sys.executable, 'host': target.hostname, 'model': os.environ['LLM_MODEL'],
        'libraries': libraries,
        'proxy_variable_names': sorted(k for k in os.environ if 'proxy' in k.lower()),
        'matches_env_file': {k: os.getenv(k) == file_config[k] for k in
                             ('LLM_MODEL', 'LLM_BASE_URL', 'LLM_API_KEY') if file_config.get(k)},
        'timeout': timeout, 'attempts_per_client': attempts, 'results': [],
    }

    def check(name, probe):
        started = monotonic()
        try:
            result = probe()
        except Exception as exc:
            result = {'ok': False, 'diagnostic': model_connection_diagnostic(exc)}
        result.update(client=name, seconds=round(monotonic() - started, 2))
        report['results'].append(result)
        emit(json.dumps(result, ensure_ascii=False))

    emit('使用同一进程配置对照；TLS 项测试直连，SDK/HTTP 项保留环境代理。')
    check('TLS_DIRECT', lambda: tls_probe(target.hostname, target.port or 443, timeout))
    for index in range(attempts):
        check(f'SDK_{index + 1}', lambda: sdk_probe(timeout))
        check(f'REQUESTS_{index + 1}', lambda: http_probe(timeout))
    report['successes'] = {prefix: sum(r['ok'] for r in report['results'] if r['client'].startswith(prefix))
                           for prefix in ('SDK_', 'REQUESTS_')}
    return report


def main():
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempts', type=int, default=2, help='每种 HTTP 客户端的尝试次数，1–3')
    parser.add_argument('--timeout', type=float, default=15)
    args = parser.parse_args()
    if not 1 <= args.attempts <= 3 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        parser.error('attempts 须为 1–3，timeout 须为 0–60 秒之间的正数')
    report = run_diagnostics(attempts=args.attempts, timeout=args.timeout)
    path = ROOT / 'outputs' / 'llm_network_diagnostic.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('成功次数：' + json.dumps(report['successes'], ensure_ascii=False))
    print(f'完整诊断（已省略密钥和异常原文）：{path}')


if __name__ == '__main__':
    main()
