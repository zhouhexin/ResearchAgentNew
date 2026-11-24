"""本机单用户评测管理入口；不把确认调用字段当成身份认证。"""
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def _loopback(host):
    try:
        address = ip_address(host or '')
        mapped = getattr(address, 'ipv4_mapped', None)
        return (mapped or address).is_loopback
    except ValueError:
        return False


def _local_host(host):
    return host == 'localhost' or _loopback(host)


def require_local_evaluation_access(request: Request):
    """限制真实连接端、Host和浏览器Origin；不信任客户端转发头。"""
    try:
        allowed = bool(request.client and _loopback(request.client.host)
                       and _local_host(request.url.hostname))
    except ValueError:
        allowed = False
    origin = request.headers.get('origin')
    if origin is not None:
        try:
            parsed = urlsplit(origin)
            parsed.port  # 同时拒绝畸形端口。
            allowed = allowed and (parsed.scheme in {'http', 'https'}
                and _local_host(parsed.hostname) and parsed.username is None
                and parsed.password is None and not parsed.path
                and not parsed.query and not parsed.fragment)
        except ValueError:
            allowed = False
    if not allowed:
        raise HTTPException(403, detail={
            'code': 'EVALUATION_LOCAL_ONLY',
            'message': '评测管理仅允许本机访问，请使用 localhost 或回环地址；远程共享部署须先配置独立认证授权。',
        })
