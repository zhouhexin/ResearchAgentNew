"""保存可定位的网络错误信息；不记录完整 URL、请求头或异常原文。"""
from urllib.parse import urlsplit

import requests


def request_diagnostic(exc):
    response = getattr(exc, 'response', None)
    request = getattr(exc, 'request', None)
    status = getattr(response, 'status_code', None)
    url = getattr(response, 'url', None) or getattr(request, 'url', None) or ''
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    causes = []
    pending = [exc]
    seen = set()
    while pending and len(seen) < 16:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        name = type(item).__name__
        if name not in causes:
            causes.append(name)
        for name in ('__cause__', '__context__', 'reason', '_reason'):
            child = getattr(item, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
        pending.extend(arg for arg in getattr(item, 'args', ()) if isinstance(arg, BaseException))
    if status is not None:
        message = f'站点返回 HTTP {status}'
        hint = {
            401: '请求未获授权，请检查该服务的 API 配置。',
            403: '站点拒绝了服务器请求；浏览器可访问不代表后端请求也获准。',
            404: '请求的页面或接口不存在。',
            429: '请求过于频繁或额度受限，请稍后重试并检查服务额度。',
        }.get(status, '请检查站点服务是否正常。')
        message += '：' + hint
    elif isinstance(exc, requests.exceptions.SSLError):
        message = 'HTTPS 证书或 TLS 握手失败，请检查证书与网络配置。'
    elif isinstance(exc, requests.exceptions.ProxyError):
        message = '代理连接失败，请检查后端进程使用的代理配置。'
    elif isinstance(exc, requests.exceptions.Timeout):
        message = '网络请求超时，请检查站点和后端网络连通性。'
    elif 'NameResolutionError' in causes or 'gaierror' in causes:
        message = '域名解析失败，请检查后端进程的 DNS 或代理设置。'
    else:
        message = '网络连接失败，请检查后端网络连通性。'
    if host:
        message = f'{host}：' + message
    return {'error_type': type(exc).__name__, 'http_status': status,
            'host': host, 'causes': causes, 'message': message}
