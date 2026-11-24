"""评测管理只信任本机连接、Host和Origin；拒绝请求不得读写产物或调用模型。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api import evaluation_workspace as api


@pytest.fixture
def app(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('被拒绝请求不应访问产物')
    monkeypatch.setattr(api, 'get', unexpected)
    monkeypatch.setattr(api, 'put', unexpected)
    app = FastAPI()
    app.include_router(api.router)
    return app


@pytest.mark.parametrize('peer', ['198.51.100.8', '192.168.1.12', '::ffff:198.51.100.8', 'testclient'])
def test_remote_peer_cannot_read_or_execute_even_with_spoofed_forward_headers(app, peer):
    client = TestClient(app, base_url='http://127.0.0.1', client=(peer,50000))
    headers = {'X-Forwarded-For':'127.0.0.1','X-Real-IP':'127.0.0.1','Origin':'http://localhost:5173'}
    for response in [client.get('/api/v1/evaluation/artifacts/experiment_private', headers=headers),
        client.post('/api/v1/evaluation/execute', headers=headers, json={'job_id':'private','confirm_model_calls':True})]:
        assert response.status_code == 403
        assert response.json()['detail']['code'] == 'EVALUATION_LOCAL_ONLY'


@pytest.mark.parametrize('origin', ['https://evil.example','http://localhost.evil.example',
    'null','http://evil@localhost:5173','http://localhost:bad','http://localhost:5173/path'])
def test_external_or_malformed_origin_is_rejected_on_loopback(app, origin):
    client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1',50000))
    response = client.post('/api/v1/evaluation/execute', headers={'Origin':origin},
                          json={'job_id':'private','confirm_model_calls':True})
    assert response.status_code == 403


@pytest.mark.parametrize('peer,base,origin', [
    ('127.0.0.1','http://127.0.0.1',None),
    ('127.0.0.1','http://localhost','http://localhost:5173'),
    ('::1','http://[::1]','http://[::1]:5173'),
    ('::ffff:127.0.0.1','http://127.0.0.1','http://127.0.0.1:5173')])
def test_local_browser_and_cli_can_read_capabilities(app, peer, base, origin):
    client = TestClient(app, base_url=base, client=(peer,50000))
    response = client.get('/api/v1/evaluation/capabilities', headers={'Origin':origin} if origin else {})
    assert response.status_code == 200 and response.json()['access_mode'] == 'loopback_only'


def test_remote_host_is_rejected_even_when_peer_is_local(app):
    client = TestClient(app, base_url='http://untrusted.example', client=('127.0.0.1',50000))
    assert client.get('/api/v1/evaluation/capabilities').status_code == 403
