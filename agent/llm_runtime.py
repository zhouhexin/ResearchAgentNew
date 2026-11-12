"""论文分析的网络设置；使用 SDK 自带 HTTP 客户端以兼容 httpx/httpx2。"""

from contextlib import contextmanager
import math
import os

from langchain_openai import ChatOpenAI
from openai import DefaultHttpxClient, APIConnectionError


def model_provider():
    provider = os.getenv('LLM_PROVIDER', 'openai').strip()
    if provider not in {'openai', 'codex'}:
        raise ValueError('LLM_PROVIDER 必须为 openai 或 codex')
    return provider


def model_name():
    if model_provider() == 'codex':
        from agent.codex_model import configured_codex_model
        return configured_codex_model() or 'Codex（CLI 默认模型）'
    return os.getenv('LLM_MODEL', '')


def model_endpoint():
    return 'codex://chatgpt' if model_provider() == 'codex' else os.getenv('LLM_BASE_URL', '')


def validate_model_configuration():
    if model_provider() == 'codex':
        from agent.codex_model import codex_executable, use_system_proxy
        codex_executable()
        use_system_proxy()
    elif not os.getenv('LLM_MODEL') or not os.getenv('LLM_API_KEY'):
        raise ValueError('请先在服务端配置 LLM_MODEL 和 LLM_API_KEY。')
    model_request_options()
    model_http_backend()


def model_http_backend(override=None):
    if model_provider() == 'codex':
        if override not in {None, 'codex'}:
            raise ValueError('Codex 模式使用本机 CLI，请选择当前连接方式；SDK/requests 仅适用于 API 模式。')
        return 'codex'
    backend = override if override is not None else os.getenv('LLM_HTTP_BACKEND', 'sdk')
    if backend not in {'sdk', 'requests'}:
        raise ValueError('LLM_HTTP_BACKEND 必须为 sdk 或 requests')
    return backend


def model_request_options():
    """只在显式配置时发送供应商扩展参数，避免影响其他兼容服务。"""
    if model_provider() == 'codex':
        return {}
    options = {}
    thinking = os.getenv('LLM_ENABLE_THINKING', '').strip().lower()
    if thinking:
        if thinking not in {'true', 'false'}:
            raise ValueError('LLM_ENABLE_THINKING 必须为 true 或 false')
        options['extra_body'] = {'enable_thinking': thinking == 'true'}
    limit = os.getenv('LLM_MAX_OUTPUT_TOKENS', '').strip()
    if limit:
        try:
            limit = int(limit)
        except ValueError:
            raise ValueError('LLM_MAX_OUTPUT_TOKENS 必须为正整数') from None
        if limit < 1:
            raise ValueError('LLM_MAX_OUTPUT_TOKENS 必须为正整数')
        options['max_tokens'] = limit
    return options


@contextmanager
def analysis_llm(*, read_timeout: float, retries: int, trust_env: bool, http_backend=None, max_output_tokens=None):
    if not math.isfinite(read_timeout) or read_timeout <= 0 or retries < 0:
        raise ValueError("超时必须为正数，重试次数不能为负数")
    if max_output_tokens is not None and (isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1):
        raise ValueError('输出 Token 上限必须为正整数')
    if model_provider() == 'codex':
        from agent.codex_model import CodexModel, configured_codex_model
        model_http_backend(http_backend)
        yield CodexModel(timeout=read_timeout, trust_env=trust_env, model=configured_codex_model())
        return
    for key in ("LLM_MODEL", "LLM_API_KEY"):
        if not os.getenv(key):
            raise ValueError(f"请在 .env 中配置 {key}")
    backend = model_http_backend(http_backend)
    network_trace = {'backend': backend}

    def trace(event, info):
        if event.startswith('connection.') and any(stage in event for stage in ('connect_tcp', 'start_tls')):
            network_trace['stage'] = event
            stream = info.get('return_value')
            if stream is not None:
                peer = stream.get_extra_info('server_addr')
                if isinstance(peer, tuple) and len(peer) >= 2:
                    network_trace.update(peer_ip=peer[0], peer_port=peer[1])

    def request_hook(request):
        network_trace.clear()
        network_trace['backend'] = backend
        request.extensions['trace'] = trace

    transport_options = {}
    if backend == 'requests':
        from agent.requests_transport import requests_transport
        transport_options['transport'] = requests_transport(trust_env=trust_env, network_trace=network_trace)
    with DefaultHttpxClient(trust_env=trust_env, timeout=read_timeout,
                            event_hooks={'request': [request_hook]}, **transport_options) as client:
        client.timeout.connect = min(10.0, read_timeout)
        client.timeout.write = min(30.0, read_timeout)
        client.timeout.pool = min(10.0, read_timeout)
        options = model_request_options()
        if max_output_tokens is not None:
            options['max_tokens'] = max_output_tokens
        if not trust_env:
            options['openai_proxy'] = ''
        llm = ChatOpenAI(
            model=os.environ["LLM_MODEL"],
            api_key=os.environ["LLM_API_KEY"],
            base_url=os.getenv("LLM_BASE_URL") or None,
            http_client=client,
            timeout=client.timeout,
            max_retries=retries,
            use_responses_api=False,
            http_socket_options=(),
            **options,
        )
        try:
            yield llm
        except APIConnectionError as exc:
            exc.model_network_trace = dict(network_trace)
            raise
