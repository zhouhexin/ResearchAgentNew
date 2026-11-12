"""提取模型连接失败的原因；不输出异常原文、密钥或完整请求地址。"""
import errno
import socket
import ssl
from urllib.parse import urlsplit


def model_connection_diagnostic(exc):
    pending, errors, seen = [exc], [], set()
    while pending and len(seen) < 16:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        errors.append(item)
        for attr in ('__cause__', '__context__', 'reason', '_reason'):
            child = getattr(item, attr, None)
            if isinstance(child, BaseException):
                pending.append(child)
        pending.extend(arg for arg in item.args if isinstance(arg, BaseException))

    names = list(dict.fromkeys(type(item).__name__ for item in errors))
    host = None
    for item in errors:
        request = getattr(item, 'request', None)
        try:
            host = urlsplit(str(getattr(request, 'url', ''))).hostname
        except ValueError:
            pass
        if host:
            break

    phase = next((phase for name, phase in (
        ('ConnectError', 'connect'), ('ReadError', 'read'), ('WriteError', 'write'),
        ('RemoteProtocolError', 'response'),
    ) if name in names), 'unknown')
    if 'ProxyError' in names:
        reason, message = 'proxy', '模型代理连接失败，请检查后端进程的代理地址、端口和代理服务。'
    elif any(isinstance(item, ssl.SSLError) or 'CERTIFICATE_VERIFY_FAILED' in str(item)
             for item in errors):
        reason, message = 'tls', '模型 HTTPS 证书校验或 TLS 握手失败，请检查后端证书和代理配置。'
    elif any(isinstance(item, socket.gaierror) for item in errors):
        reason, message = 'dns', '模型服务域名解析失败，请检查 LLM_BASE_URL、后端 DNS 和代理配置。'
    elif any(isinstance(item, ConnectionRefusedError) or getattr(item, 'errno', None) == errno.ECONNREFUSED
             for item in errors):
        reason, message = 'refused', '模型服务或代理端口拒绝连接，请检查服务是否启动、地址和端口是否正确。'
    elif any(isinstance(item, PermissionError) for item in errors):
        reason, message = 'permission', '后端进程的网络访问被拒绝，请检查运行环境的网络权限。'
    elif any(name in names for name in ('RemoteProtocolError', 'ReadError', 'WriteError', 'ConnectionResetError')):
        reason = 'interrupted'
        message = ('建立模型连接时被重置，尚未收到模型响应。请检查模型网关、代理和网络链路。'
                   if phase == 'connect' else '模型连接在请求期间中断，请检查模型服务或代理的稳定性后继续未完成批次。')
    elif any(name in names for name in ('ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout', 'TimeoutError')):
        reason, message = 'timeout', '模型网络连接超时，请检查后端网络、代理和模型服务状态。'
    else:
        reason, message = 'connection', '无法连接模型服务，请检查后端网络、代理及模型地址。'
    if host:
        message = f'{host}：{message}'
    diagnostic = {'reason': reason, 'phase': phase, 'host': host, 'causes': names, 'message': message}
    for item in errors:
        trace = getattr(item, 'model_network_trace', None)
        if isinstance(trace, dict):
            diagnostic['network'] = {key: trace[key] for key in ('backend', 'stage', 'peer_ip', 'peer_port') if key in trace}
            break
    return diagnostic


def model_timeout_diagnostic(exc, read_timeout):
    diagnostic = model_connection_diagnostic(exc)
    names = diagnostic['causes']
    phase = next((phase for name, phase in (
        ('ConnectTimeout', 'connect'), ('ReadTimeout', 'read'),
        ('WriteTimeout', 'write'), ('PoolTimeout', 'pool'),
    ) if name in names), 'unknown')
    diagnostic.update(reason='timeout', phase=phase, read_timeout=read_timeout)
    descriptions = {
        'connect': '建立模型连接超时，请检查模型地址、网络和代理。',
        'read': f'等待模型响应超时（读取超时设置 {read_timeout:g} 秒），请检查模型思考模式、输出长度或服务负载。',
        'write': '发送模型请求超时，请检查后端网络和代理。',
        'pool': '等待模型连接池超时，请检查并发请求数量。',
        'unknown': f'模型请求超时（读取超时设置 {read_timeout:g} 秒），底层接口未提供具体超时阶段。',
    }
    diagnostic['message'] = descriptions[phase]
    return diagnostic
