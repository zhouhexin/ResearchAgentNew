from pathlib import Path

import pytest
import requests

from tools import open_access_pdf as oa


class Response:
    def __init__(self, data=None, content=b'', status=200, headers=None):
        self.data, self.content, self.status_code = data, content, status
        self.headers = headers or {}
        self.is_redirect = status in {301, 302, 303, 307, 308}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('private response details')
    def json(self):
        return self.data
    def iter_content(self, chunk_size):
        # 刻意分割 PDF 文件头，验证不依赖网络分块边界。
        yield self.content[:2]
        yield self.content[2:]


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(oa.socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('93.184.216.34', 443))])


def test_only_open_locations_and_identity_are_accepted(monkeypatch):
    work = {'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/10.1234/test',
            'best_oa_location': {'is_oa': True, 'pdf_url': 'https://example.org/open.pdf'},
            'locations': [
                {'is_oa': False, 'pdf_url': 'https://example.org/closed.pdf'},
                {'is_oa': True, 'pdf_url': 'https://example.org/open.pdf'},
                {'is_oa': True, 'landing_page_url': 'https://arxiv.org/abs/2601.12345'},
            ]}
    monkeypatch.setattr(oa.requests, 'get', lambda *a, **k: Response(data=work))
    urls = oa.find_open_pdf_urls('10.1234/test', 'W123')
    assert urls == ['https://example.org/open.pdf', 'https://arxiv.org/pdf/2601.12345']
    with pytest.raises(oa.FulltextError, match='标识不一致'):
        oa.find_open_pdf_urls('10.1234/other', 'W123')
    with pytest.raises(oa.FulltextError, match='缺少'):
        oa.find_open_pdf_urls()


def test_closed_or_missing_pdf_gives_actionable_error(monkeypatch):
    monkeypatch.setattr(oa.requests, 'get', lambda *a, **k: Response(data={
        'id': 'https://openalex.org/W123', 'locations': [{'is_oa': False, 'pdf_url': 'https://example.org/paywall.pdf'}],
    }))
    with pytest.raises(oa.FulltextError) as error:
        oa.find_open_pdf_urls(openalex_id='W123')
    assert error.value.code == 'NO_OPEN_PDF'


def test_download_and_public_redirect(public_dns, monkeypatch, tmp_path):
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        assert kwargs['allow_redirects'] is False
        return Response(status=302, headers={'Location': '/actual.pdf'}) if len(seen) == 1 else Response(content=b'%PDF-valid')
    monkeypatch.setattr(oa.requests, 'get', get)
    destination = tmp_path / 'paper.pdf'
    assert oa.download_open_pdf('https://example.org/start', destination) == 'https://example.org/actual.pdf'
    assert destination.read_bytes() == b'%PDF-valid'


@pytest.mark.parametrize('url', ['http://example.org/a.pdf', 'https://user:pass@example.org/a.pdf', 'https://example.org:8443/a.pdf'])
def test_bad_url_rejected(url):
    with pytest.raises(oa.FulltextError):
        oa.validate_pdf_url(url)


def test_private_redirect_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr(oa.socket, 'getaddrinfo', lambda host, *a, **k: [(2, 1, 6, '', (
        '127.0.0.1' if host == 'localhost' else '93.184.216.34', 443))])
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        return Response(status=302, headers={'Location': 'https://localhost/private'})
    monkeypatch.setattr(oa.requests, 'get', get)
    destination = tmp_path / 'paper.pdf'
    with pytest.raises(oa.FulltextError, match='非公开网络'):
        oa.download_open_pdf('https://example.org/a.pdf', destination)
    assert len(seen) == 1 and not destination.exists()


@pytest.mark.parametrize('content,limit,code', [(b'<html>login</html>', 100, 'NOT_A_PDF'), (b'%PDF-too-large', 6, 'PDF_TOO_LARGE')])
def test_invalid_or_oversized_download_removed(public_dns, monkeypatch, tmp_path, content, limit, code):
    monkeypatch.setattr(oa, 'MAX_BYTES', limit)
    monkeypatch.setattr(oa.requests, 'get', lambda *a, **k: Response(content=content))
    destination = tmp_path / 'paper.pdf'
    with pytest.raises(oa.FulltextError) as error:
        oa.download_open_pdf('https://example.org/a.pdf', destination)
    assert error.value.code == code
    assert not destination.exists()


@pytest.mark.parametrize('budget,success', [(600,True),(120,False)])
def test_slow_stream_can_exceed_old_120_second_limit(public_dns, monkeypatch, tmp_path, budget, success):
    from types import SimpleNamespace
    clock=[0.0]
    monkeypatch.setattr(oa,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    monkeypatch.setenv('PDF_DOWNLOAD_TIMEOUT',str(budget))
    class Slow(Response):
        def iter_content(self, chunk_size):
            yield b'%PDF-'
            clock[0]=150.0
            yield b'slow-valid-body'
    monkeypatch.setattr(oa.requests,'get',lambda *a,**k:Slow())
    path=tmp_path/'paper.pdf';events=[]
    if success:
        assert oa.download_open_pdf('https://example.org/file.pdf',path,progress=events.append)
        assert path.read_bytes()==b'%PDF-slow-valid-body'
        assert events[-1]['phase']=='completed' and events[-1]['elapsed_seconds']==150
    else:
        with pytest.raises(oa.FulltextError) as failure:
            oa.download_open_pdf('https://example.org/file.pdf',path)
        assert failure.value.code=='PDF_DOWNLOAD_TIMEOUT'
        assert failure.value.diagnostic['received_bytes']>0
        assert failure.value.diagnostic['total_timeout']==120 and not path.exists()


@pytest.mark.parametrize('kind,code', [('connect','PDF_CONNECT_TIMEOUT'),('read','PDF_READ_TIMEOUT'),('wrapped','PDF_READ_TIMEOUT')])
def test_timeout_phases_and_secret_redaction(public_dns,monkeypatch,tmp_path,kind,code):
    class Stalled(Response):
        def iter_content(self, chunk_size):
            yield b'%PDF-data'
            if kind=='wrapped':
                from urllib3.exceptions import ReadTimeoutError
                raise requests.ConnectionError(ReadTimeoutError(None,'https://example.org/private?token=secret-key','secret-key'))
            raise requests.ReadTimeout('secret-key')
    def get(*a,**k):
        if kind=='connect':raise requests.ConnectTimeout('secret-key')
        return Stalled()
    monkeypatch.setattr(oa.requests,'get',get)
    with pytest.raises(oa.FulltextError) as failure:
        oa.download_open_pdf('https://example.org/private?token=secret-key',tmp_path/'paper.pdf')
    assert failure.value.code==code
    details=failure.value.diagnostic
    assert details['host']=='example.org' and details['phase']==('connect' if kind=='connect' else 'download')
    assert details['received_bytes']==(0 if kind=='connect' else 9)
    import json
    assert 'secret-key' not in json.dumps(details) and '/private' not in json.dumps(details)
    assert not (tmp_path/'paper.pdf').exists()


@pytest.mark.parametrize('name,value', [('PDF_DOWNLOAD_TIMEOUT','nan'),('PDF_READ_TIMEOUT','0'),
    ('PDF_CONNECT_TIMEOUT','-1'),('PDF_DOWNLOAD_TIMEOUT','3601'),('PDF_READ_TIMEOUT','bad')])
def test_download_policy_rejects_invalid_settings(monkeypatch,name,value):
    monkeypatch.setenv(name,value)
    with pytest.raises(ValueError,match=name):oa.pdf_download_settings()


def test_non_pdf_is_rejected_before_reading_rest_of_page(public_dns,monkeypatch,tmp_path):
    class HTML(Response):
        def iter_content(self, chunk_size):
            yield b'<html'
            pytest.fail('应在第一个非PDF文件头时停止，不必等待整个错误页面')
    monkeypatch.setattr(oa.requests,'get',lambda *a,**k:HTML())
    with pytest.raises(oa.FulltextError) as failure:
        oa.download_open_pdf('https://example.org/file.pdf',tmp_path/'paper.pdf')
    assert failure.value.code=='NOT_A_PDF' and not (tmp_path/'paper.pdf').exists()


@pytest.mark.parametrize('body,content_type,verified', [(b'', 'application/pdf', False),
    (b'%PDF-data', 'application/octet-stream', True), (b'%P', 'application/pdf', False)])
def test_timeout_evidence_distinguishes_header_claim_from_pdf_bytes(public_dns,monkeypatch,tmp_path,body,content_type,verified):
    class Partial(Response):
        def iter_content(self,chunk_size):
            if body:yield body
            raise requests.ReadTimeout('secret')
    monkeypatch.setattr(oa.requests,'get',lambda *a,**k:Partial(headers={'Content-Type':content_type}))
    with pytest.raises(oa.FulltextError) as failure:
        oa.download_open_pdf('https://example.org/file.pdf',tmp_path/'paper.pdf')
    d=failure.value.diagnostic
    assert d['candidate_found'] is True and d['response_received'] is True and d['http_status']==200
    assert d['pdf_header_verified'] is verified and d['received_bytes']==len(body)
    assert d['content_type']==content_type
    assert ('已确认 PDF 文件头' in str(failure.value)) is verified


def test_html_response_does_not_claim_pdf_found(public_dns,monkeypatch,tmp_path):
    monkeypatch.setattr(oa.requests,'get',lambda *a,**k:Response(content=b'<html>login</html>',headers={'Content-Type':'text/html'}))
    with pytest.raises(oa.FulltextError) as failure:
        oa.download_open_pdf('https://example.org/file.pdf',tmp_path/'paper.pdf')
    assert failure.value.code=='NOT_A_PDF'
    d=failure.value.diagnostic
    assert d['response_received'] is True and d['pdf_header_verified'] is False and d['content_type']=='text/html'
