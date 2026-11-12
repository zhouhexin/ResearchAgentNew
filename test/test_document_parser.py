from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

from tools.document_parser import PDF_PARSER, build_document, parse_pdf, parse_webpage
from tools.document_verification import verify_pdf


def test_real_pdf_all_chunks():
    path = next(iter(sorted((Path(__file__).resolve().parents[1] / "data/pdfs").glob("*.pdf"))), None)
    if path is None:
        pytest.skip("没有本地真实 PDF")
    document = parse_pdf(path, chunk_size=300)
    result = verify_pdf(document, path)
    assert result["all_pages_match_pdf"]
    assert len(document.content_sha256) == 64


def test_pdf_preserves_blank_page_numbers(tmp_path):
    path = tmp_path / "blank-middle.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "First page evidence")
        pdf.new_page()
        pdf.new_page().insert_text((72, 72), "Third page evidence")
        pdf.save(path)
    document = parse_pdf(path)
    assert document.page_count == 3
    assert [chunk.page_number for chunk in document.chunks] == [1, 3]
    verify_pdf(document, path)


def test_two_column_pdf_keeps_each_paragraph_contiguous(tmp_path):
    path = tmp_path / 'two-columns.pdf'
    left = 'Left column first sentence.\nLeft column second sentence.'
    right = 'Right column first sentence.\nRight column second sentence.'
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_textbox((50, 100, 280, 300), left)
        page.insert_textbox((320, 100, 550, 300), right)
        pdf.save(path)
    document = parse_pdf(path)
    assert left in document.text and right in document.text
    assert document.text.index('Left column second') < document.text.index('Right column first')
    assert document.parser == PDF_PARSER
    assert verify_pdf(document, path)['all_pages_match_pdf']
    # 已保存的旧版文本仍可按其原提取规则验收。
    with pymupdf.open(path) as pdf:
        legacy = build_document([(1, pdf[0].get_text('text', sort=True))], source_url=path.as_uri(),
                                source_type='pdf', parser='pymupdf', page_count=1)
    assert verify_pdf(legacy, path)['all_pages_match_pdf']


def test_web_chunks_have_no_page(monkeypatch):
    content = "# Introduction\n" + "测试网页正文。" * 100
    monkeypatch.setattr("tools.document_parser.read_webpage", lambda url: content)
    document = parse_webpage("https://example.org", chunk_size=100)
    document.validate()
    assert document.page_count is None
    assert all(chunk.page_number is None for chunk in document.chunks)
    assert document.text == content


@pytest.mark.parametrize("kind", ["negative", "overflow", "wrong_text", "overlap", "page", "index", "gap"])
def test_invalid_evidence_rejected(kind):
    document = build_document(
        [(1, "A" * 250)], source_url="file:///sample.pdf",
        source_type="pdf", parser="test", page_count=1, chunk_size=100,
    )
    chunk = document.chunks[0]
    changes = {
        "negative": {"start_char": -1},
        "overflow": {"end_char": 1000},
        "wrong_text": {"text": "wrong"},
        "page": {"page_number": 2},
        "index": {"index": 3},
        "gap": {"start_char": 1, "text": "A" * 99},
    }
    if kind == "overlap":
        end = document.chunks[1].end_char
        start = document.chunks[0].end_char - document.chunk_overlap - 1
        document.chunks[1] = replace(document.chunks[1], start_char=start, text=document.text[start:end])
    else:
        document.chunks[0] = replace(chunk, **changes[kind])
    with pytest.raises(ValueError):
        document.validate()


def test_empty_document_rejected():
    with pytest.raises(ValueError, match="没有提取到正文"):
        build_document([(1, "  ")], source_url="file:///empty.pdf", source_type="pdf", parser="test", page_count=1)


def test_web_rejects_fake_page():
    document = build_document([(None, "网页正文")], source_url="https://example.org", source_type="web", parser="test")
    document.chunks[0].page_number = 1
    with pytest.raises(ValueError, match="网页片段"):
        document.validate()


def test_overlapping_windows_preserve_offsets_and_never_cross_pdf_pages():
    pages = [(1, ' '.join(f'First page sentence {i}.' for i in range(20))),
             (2, ' '.join(f'Second page sentence {i}.' for i in range(20)))]
    document = build_document(pages, source_url='file:///windows.pdf', source_type='pdf',
                              parser='test', page_count=2, chunk_size=120, chunk_overlap=40)
    document.validate()
    assert document.text == '\n\n'.join(text for _, text in pages)
    assert any(a.end_char > b.start_char for a, b in zip(document.chunks, document.chunks[1:]))
    for a, b in zip(document.chunks, document.chunks[1:]):
        if a.end_char > b.start_char:
            assert a.page_number == b.page_number and a.end_char - b.start_char <= 40
    assert all(document.text[c.start_char:c.end_char] == c.text for c in document.chunks)


def test_duplicate_or_nested_windows_rejected():
    document = build_document([(None, 'A' * 250)], source_url='https://example.org',
                              source_type='web', parser='test', chunk_size=100, chunk_overlap=20)
    document.chunks[1] = replace(document.chunks[0], index=1)
    with pytest.raises(ValueError, match='没有向前推进'):
        document.validate()


@pytest.mark.parametrize('source_type', ['pdf','web'])
def test_nul_replacement_precedes_chunk_offsets_and_postgres_adaptation(source_type):
    from psycopg.adapt import Transformer, PyFormat
    from psycopg import DataError
    raw='Introduction\nFirst\x00word. '+('A complete sentence. '*12)+'End\x00.'
    with pytest.raises(DataError):Transformer().get_dumper(raw,PyFormat.TEXT).dump(raw)
    document=build_document([(1 if source_type=='pdf' else None,raw)],source_url='https://example.org/paper',
                            source_type=source_type,parser='test',page_count=1 if source_type=='pdf' else None,chunk_size=100)
    assert document.text==raw.replace('\x00','\ufffd') and len(document.text)==len(raw)
    assert 'First\ufffdword' in document.text
    assert '\x00' not in document.text and all('\x00' not in c.text for c in document.chunks)
    for chunk in document.chunks:
        assert document.text[chunk.start_char:chunk.end_char]==chunk.text
        Transformer().get_dumper(chunk.text,PyFormat.TEXT).dump(chunk.text)
    Transformer().get_dumper(document.text,PyFormat.TEXT).dump(document.text)
    document.validate()
