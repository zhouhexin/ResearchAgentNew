import json

import pytest

from tools import eth_pdf
from tools.open_access_pdf import FulltextError
from test.test_open_access_pdf import Response, public_dns

PAGE = 'https://www.research-collection.ethz.ch/entities/publication/bb4e8474-aa7b-4c89-a123-0b5868b177d2'
DOI = '10.3929/ethz-c-000800472'
ITEM = PAGE.rsplit('/', 1)[-1]
BUNDLE = '11111111-1111-1111-1111-111111111111'
STREAM = '22222222-2222-2222-2222-222222222222'
ORIGIN = 'https://www.research-collection.ethz.ch'


def repository_response(url, doi=DOI):
    if url.endswith('/items/' + ITEM):
        return {'uuid': ITEM, 'metadata': {'dc.identifier.doi': [{'value': doi}]}}
    if '/bundles?' in url:
        return {'_embedded': {'bundles': [{'uuid': BUNDLE, 'name': 'ORIGINAL'}, {'uuid': BUNDLE, 'name': 'THUMBNAIL'}]}}
    if '/bitstreams?' in url:
        return {'_embedded': {'bitstreams': [{'uuid': STREAM, 'name': 'paper.pdf'}, {'uuid': BUNDLE, 'name': 'supporting.pdf'}]}}
    raise AssertionError(url)


def test_original_pdf_only_with_doi_verified(public_dns, monkeypatch):
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        return Response(content=json.dumps(repository_response(url)).encode())
    monkeypatch.setattr(eth_pdf.requests, 'get', get)
    assert eth_pdf.find_eth_pdf_urls(PAGE, DOI) == [f'{ORIGIN}/server/api/core/bitstreams/{STREAM}/content']
    assert len(seen) == 3


def test_wrong_doi_cannot_read_item_files(public_dns, monkeypatch):
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        return Response(content=json.dumps(repository_response(url, '10.1234/other')).encode())
    monkeypatch.setattr(eth_pdf.requests, 'get', get)
    with pytest.raises(FulltextError) as error:
        eth_pdf.find_eth_pdf_urls(PAGE, DOI)
    assert error.value.code == 'PAPER_ID_MISMATCH'
    assert len(seen) == 1


def test_unrelated_site_not_treated_as_eth_repository():
    assert eth_pdf.find_eth_pdf_urls('https://example.org/entities/publication/' + ITEM, DOI) == []
