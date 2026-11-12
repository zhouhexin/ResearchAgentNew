import json
import socket
import ssl
import pytest
from openai import APIConnectionError

from agent.model_diagnostics import model_connection_diagnostic, model_timeout_diagnostic


@pytest.mark.parametrize('library', ['httpx', 'httpx2'])
@pytest.mark.parametrize('kind', ['dns', 'proxy', 'tls', 'refused', 'interrupted', 'connection'])
def test_wrapped_transport_errors_have_safe_actionable_diagnostics(library, kind):
    http = pytest.importorskip(library)
    request = http.Request('POST', 'https://user:password@model.example.org/private?api_key=secret-key')
    if kind == 'proxy':
        cause = http.ProxyError('proxy password secret-key')
    elif kind == 'interrupted':
        cause = http.RemoteProtocolError('private upstream details')
    else:
        cause = http.ConnectError('private transport details secret-key')
        roots = {'dns': socket.gaierror('private DNS details'),
                 'tls': ssl.SSLCertVerificationError('private certificate details'),
                 'refused': ConnectionRefusedError('private proxy port')}
        cause.__cause__ = roots.get(kind)
    exc = APIConnectionError(request=request)
    exc.__cause__ = cause
    diagnostic = model_connection_diagnostic(exc)
    assert diagnostic['reason'] == kind
    assert diagnostic['host'] == 'model.example.org'
    assert type(cause).__name__ in diagnostic['causes']
    serialized = json.dumps(diagnostic)
    assert all(secret not in serialized for secret in ('password', 'secret-key', 'private', 'api_key', 'user:'))


def test_exception_cycles_are_bounded():
    exc = APIConnectionError(request=None)
    exc.__cause__ = exc
    assert model_connection_diagnostic(exc)['causes'] == ['APIConnectionError']


@pytest.mark.parametrize('library', ['httpx', 'httpx2'])
def test_reset_during_connect_is_not_reported_as_response_interruption(library):
    http = pytest.importorskip(library)
    exc = APIConnectionError(request=http.Request('POST', 'https://model.example.org'))
    exc.__cause__ = http.ConnectError('private details')
    exc.__cause__.__cause__ = ConnectionResetError(54, 'private details')
    result = model_connection_diagnostic(exc)
    assert result['reason'] == 'interrupted' and result['phase'] == 'connect'
    assert '建立模型连接时被重置' in result['message']
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('library', ['httpx', 'httpx2'])
@pytest.mark.parametrize('name,phase', [('ReadTimeout', 'read'), ('ConnectTimeout', 'connect'),
                                      ('WriteTimeout', 'write'), ('PoolTimeout', 'pool')])
def test_timeout_phase_survives_sdk_wrapper(library, name, phase):
    from openai import APITimeoutError
    http = pytest.importorskip(library)
    exc = APITimeoutError(request=http.Request('POST', 'https://model.example.org'))
    exc.__cause__ = getattr(http, name)('private credentials')
    result = model_timeout_diagnostic(exc, 400)
    assert result['phase'] == phase and result['read_timeout'] == 400
    assert name in result['causes']
    assert 'private' not in json.dumps(result)
