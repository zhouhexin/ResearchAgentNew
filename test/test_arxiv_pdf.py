import pytest

from tools import arxiv_pdf as arxiv
from tools.open_access_pdf import FulltextError


ATOM = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/1706.03762v7</id>
<title>Attention Is All You Need</title><author><name>Ashish Vaswani</name></author>{doi}</entry></feed>'''


@pytest.mark.parametrize('value,expected', [
    ('https://arxiv.org/abs/1706.03762', '1706.03762'),
    ('https://arxiv.org/pdf/1706.03762v7.pdf', '1706.03762v7'),
    ('https://arxiv.org/abs/hep-th/9901001', 'hep-th/9901001'),
    ('10.48550/arxiv.1706.03762', '1706.03762'),
    ('https://example.org/abs/1706.03762', None),
    ('https://arxiv.org/abs/not-a-paper', None),
])
def test_explicit_arxiv_ids(value, expected):
    assert arxiv.arxiv_id(value) == expected


def test_title_and_author_must_both_match(monkeypatch):
    monkeypatch.setattr(arxiv, 'query_arxiv', lambda p: ATOM.format(doi='').encode())
    assert arxiv.find_arxiv_pdf_urls('Attention Is All You Need', ['Ashish Vaswani']) == ['https://arxiv.org/pdf/1706.03762v7']
    assert arxiv.find_arxiv_pdf_urls('Attention is all you need!', ['Vaswani, Ashish'])
    with pytest.raises(FulltextError):
        arxiv.find_arxiv_pdf_urls('A different paper', ['Ashish Vaswani'])
    with pytest.raises(FulltextError):
        arxiv.find_arxiv_pdf_urls('Attention Is All You Need', ['Another Author'])
    with pytest.raises(FulltextError):
        arxiv.find_arxiv_pdf_urls('Attention Is All You Need', [])


def test_conflicting_doi_is_rejected(monkeypatch):
    monkeypatch.setattr(arxiv, 'query_arxiv', lambda p: ATOM.format(doi='<arxiv:doi>10.1234/other</arxiv:doi>').encode())
    with pytest.raises(FulltextError):
        arxiv.find_arxiv_pdf_urls('Attention Is All You Need', ['Ashish Vaswani'], doi='10.1234/expected')


def test_direct_id_and_version_match(monkeypatch):
    params = []
    def query(p):
        params.append(p)
        return ATOM.format(doi='').encode()
    monkeypatch.setattr(arxiv, 'query_arxiv', query)
    assert arxiv.find_arxiv_pdf_urls(source_url='https://arxiv.org/abs/1706.03762v7')
    assert params[0]['id_list'] == '1706.03762v7'
    with pytest.raises(FulltextError):
        arxiv.find_arxiv_pdf_urls(source_url='https://arxiv.org/abs/1706.03762v6')
    with pytest.raises(FulltextError):
        arxiv.find_arxiv_pdf_urls(source_url='https://arxiv.org/abs/2601.12345')


def test_bad_arxiv_xml_is_actionable(monkeypatch):
    monkeypatch.setattr(arxiv, 'query_arxiv', lambda p: b'<html>bad gateway')
    with pytest.raises(FulltextError) as error:
        arxiv.find_arxiv_pdf_urls('Attention Is All You Need', ['Ashish Vaswani'])
    assert error.value.code == 'ARXIV_RESPONSE_ERROR'
