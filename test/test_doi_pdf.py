import pytest

from tools import doi_pdf
from tools.open_access_pdf import FulltextError
from test.test_open_access_pdf import Response, public_dns


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch, tmp_path):
    monkeypatch.setenv('RESEARCHPILOT_FULLTEXT_SOURCES_FILE', str(tmp_path / 'sources.json'))


def test_repository_citation_pdf_and_relative_link(public_dns, monkeypatch):
    def get(url, **kwargs):
        if url.startswith('https://doi.org/'):
            return Response(status=302, headers={'Location': 'https://repository.example.org/items/paper'})
        return Response(content=b'<html><meta name="citation_pdf_url" content="/bitstreams/123/download"></html>')
    monkeypatch.setattr(doi_pdf.requests, 'get', get)
    assert doi_pdf.find_doi_pdf_urls('10.3929/ethz-c-000800472') == ['https://repository.example.org/bitstreams/123/download']


def test_no_pdf_does_not_download_references(public_dns, monkeypatch):
    monkeypatch.setattr(doi_pdf.requests, 'get', lambda *a, **k: Response(content=b'<a href="https://example.org/references.pdf">References</a>'))
    with pytest.raises(FulltextError) as error:
        doi_pdf.find_doi_pdf_urls('10.1234/example')
    assert error.value.code == 'NO_DOI_PDF'


def test_direct_pdf_recognized(public_dns, monkeypatch):
    monkeypatch.setattr(doi_pdf.requests, 'get', lambda *a, **k: Response(content=b'%PDF-example'))
    assert doi_pdf.find_doi_pdf_urls('10.1234/example') == ['https://doi.org/10.1234/example']


def test_visible_eth_download_link_without_pdf_metadata(public_dns, monkeypatch):
    def get(url, **kwargs):
        if url.startswith('https://doi.org/'):
            return Response(status=302, headers={'Location': 'https://www.research-collection.ethz.ch/entities/publication/example'})
        return Response(content=b'''<html><body><h1>Paper</h1>
          <a href="/bitstreams/1234-abcd/download"><span>Download</span> (PDF)</a>
        </body></html>''')
    monkeypatch.setattr(doi_pdf.requests, 'get', get)
    assert doi_pdf.find_doi_pdf_urls('10.3929/ethz-c-000800472') == ['https://www.research-collection.ethz.ch/bitstreams/1234-abcd/download']


def test_file_button_base_url_and_reference_exclusion():
    parser = doi_pdf.PdfMetadata()
    parser.feed('''<html><head><base href="/"></head><body>
      <section id="references"><a href="references.pdf" download>Download PDF</a></section>
      <section class="related-publications"><a href="/bitstreams/other/download">PDF</a></section>
      <a href="article.pdf" download><span>Main document PDF</span></a>
      <a href="/supplement.pdf" download>Supporting information</a>
      <a href="https://external.example.org/other.pdf" download>Download PDF</a>
    </body></html>''')
    assert parser.pdf_urls('https://repository.example.org/entities/publication/id') == ['https://repository.example.org/article.pdf']


def test_metadata_links_take_priority_and_are_deduplicated():
    parser = doi_pdf.PdfMetadata()
    parser.feed('''<meta name="citation_pdf_url" content="/primary.pdf">
    <a href="/other.pdf" download>Download PDF</a>
    <a href="/primary.pdf" download>Full text PDF</a>''')
    assert parser.pdf_urls('https://repository.example.org/item') == ['https://repository.example.org/primary.pdf', 'https://repository.example.org/other.pdf']
