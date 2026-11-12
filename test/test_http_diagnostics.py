import json
import socket

import requests

from tools.http_diagnostics import request_diagnostic


def test_http_error_retains_status_and_host_without_secrets():
    response = requests.Response()
    response.status_code = 403
    response.url = 'https://example.org/private/path?api_key=secret-value'
    exc = requests.HTTPError('secret-value password', response=response)
    result = request_diagnostic(exc)
    assert result['http_status'] == 403
    assert result['host'] == 'example.org'
    assert 'HTTP 403' in result['message']
    assert 'secret-value' not in json.dumps(result)
    assert '/private/path' not in json.dumps(result)


def test_dns_and_timeout_are_distinguished():
    exc = requests.ConnectionError('private credentials')
    exc.__cause__ = socket.gaierror('private DNS details')
    assert '域名解析失败' in request_diagnostic(exc)['message']
    assert '超时' in request_diagnostic(requests.ReadTimeout('secret'))['message']
