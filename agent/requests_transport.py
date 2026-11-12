"""用 requests 承载 SDK 的非流式 HTTP 请求；保留 SDK 与 LangChain 的解析流程。"""
from importlib import import_module

from openai import DefaultHttpxClient
import requests

from agent.model_diagnostics import model_connection_diagnostic


def requests_transport(*, trust_env, network_trace):
    # SDK 2/3 分别可能使用 httpx/httpx2；Transport 和 Response 必须与 SDK 同库。
    library = next(cls.__module__.split('.')[0] for cls in DefaultHttpxClient.__mro__
                   if cls.__module__.split('.')[0] in {'httpx', 'httpx2'})
    http = import_module(library)

    class RequestsTransport(http.BaseTransport):
        def __init__(self):
            self.session = requests.Session()
            self.session.trust_env = trust_env

        def handle_request(self, request):
            timeout = request.extensions.get('timeout', {})
            received = False
            network_trace.update(backend='requests', stage='requests.send')
            try:
                with self.session.request(
                    request.method, str(request.url), headers=dict(request.headers),
                    data=request.read(), timeout=(timeout.get('connect', 10), timeout.get('read', 400)),
                    allow_redirects=False, stream=True,
                    auth=lambda prepared: prepared,  # 保留 SDK 的鉴权头，禁止 .netrc 覆盖。
                ) as response:
                    received = True
                    network_trace['stage'] = 'requests.read'
                    content = response.content
                    # requests 已解压响应，不能让 SDK 的 HTTP 客户端再次解压。
                    headers = [(key, value) for key, value in response.headers.items()
                               if key.lower() not in {'content-encoding', 'content-length', 'transfer-encoding'}]
                    network_trace['stage'] = 'requests.complete'
                    return http.Response(response.status_code, headers=headers, content=content, request=request)
            except requests.ConnectTimeout as exc:
                raise http.ConnectTimeout('连接超时', request=request) from exc
            except requests.Timeout as exc:
                raise http.ReadTimeout('响应读取超时', request=request) from exc
            except requests.RequestException as exc:
                causes = model_connection_diagnostic(exc)['causes']
                if received and 'ReadTimeoutError' in causes:
                    raise http.ReadTimeout('响应读取超时', request=request) from exc
                error_type = http.ReadError if received else http.ConnectError
                raise error_type('HTTP 传输失败', request=request) from exc

        def close(self):
            self.session.close()

    return RequestsTransport()
