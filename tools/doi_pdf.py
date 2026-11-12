"""从 DOI 跳转后的原站页面读取明确标注的全文 PDF，不抓取引用链接。"""
from html.parser import HTMLParser
import re
import json
import os
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests

from storage.repository import normalize_doi
from tools.open_access_pdf import FulltextError, validate_pdf_url
from tools.eth_pdf import eth_item_id, find_eth_pdf_urls

MAX_HTML_BYTES = 2 * 1024 * 1024


def registered_source(doi):
    path = Path(os.getenv('RESEARCHPILOT_FULLTEXT_SOURCES_FILE') or Path(__file__).resolve().parents[1] / 'data' / 'fulltext_sources.json')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        url = data.get(doi, '') if isinstance(data, dict) else ''
        return url if isinstance(url, str) and eth_item_id(url) else ''
    except (OSError, ValueError):
        return ''


class PdfMetadata(HTMLParser):
    VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    EXCLUDED = re.compile(r'references|bibliography|citations|related[-_ ]|supplement|supporting[-_ ]|appendix', re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []
        self.anchors = []
        self.stack = []
        self.anchor = None
        self.base = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        excluded = bool(self.EXCLUDED.search((attrs.get('id') or '') + ' ' + (attrs.get('class') or '')))
        excluded = excluded or any(blocked for _, blocked in self.stack)
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, excluded))
        if tag == 'base' and attrs.get('href'):
            self.base = attrs['href']
        elif tag == 'meta' and (attrs.get('name') or '').lower() in {'citation_pdf_url', 'eprints.document_url'}:
            value = attrs.get('content')
            if value:
                self.urls.append(value)
        elif tag == 'link' and (attrs.get('type') or '').lower() == 'application/pdf':
            if 'alternate' in (attrs.get('rel') or '').lower().split() and attrs.get('href'):
                self.urls.append(attrs['href'])
        elif tag == 'a' and attrs.get('href') and not excluded:
            self.anchor = {'attrs': attrs, 'text': []}

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor['text'].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.anchors.append(self.anchor)
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def pdf_urls(self, page_url):
        base_url = page_url
        if self.base:
            candidate = urljoin(page_url, self.base)
            if urlsplit(candidate).netloc == urlsplit(page_url).netloc:
                base_url = candidate
        urls = [urljoin(base_url, raw) for raw in self.urls]
        scored = []
        for anchor in self.anchors:
            attrs = anchor['attrs']
            url = urljoin(base_url, attrs['href'])
            parsed = urlsplit(url)
            label = ' '.join(anchor['text']) + ' ' + (attrs.get('title') or '') + ' ' + (attrs.get('aria-label') or '')
            if self.EXCLUDED.search(label + ' ' + parsed.path):
                continue
            # 正文按钮仅采用同站文件，避免把外部参考文献或推荐论文误作全文。
            if parsed.netloc != urlsplit(page_url).netloc:
                continue
            repository_file = bool(re.search(r'/(?:bitstreams/[^/]+/(?:download|content)|bitstream/(?:handle/)?[^?]+|server/api/core/bitstreams/[^/]+/content)$', parsed.path, re.I))
            is_pdf = parsed.path.lower().endswith('.pdf') or attrs.get('type', '').lower() == 'application/pdf'
            download_label = bool(re.search(r'\b(?:download|pdf|full[ -]?text)\b|下载|全文', label, re.I))
            explicit_download = 'download' in attrs or attrs.get('type', '').lower() == 'application/pdf'
            if not (repository_file or (is_pdf and (download_label or explicit_download))):
                continue
            score = 2 if re.search(r'full[ -]?text|main document|published version|accepted manuscript|全文|正文', label, re.I) else 1
            scored.append((score, url))
        urls.extend(url for _, url in sorted(scored, key=lambda row: row[0], reverse=True))
        return list(dict.fromkeys(url for url in urls if url.startswith('https://')))


def find_doi_pdf_urls(doi, source_url=''):
    doi = normalize_doi(doi)
    if not doi:
        raise FulltextError('NO_DOI_PDF', '当前记录没有 DOI，无法查询原文网站。')
    source_url = source_url if eth_item_id(source_url) else registered_source(doi)
    if eth_item_id(source_url):
        try:
            return find_eth_pdf_urls(source_url, doi)
        except FulltextError as exc:
            if exc.code == 'PAPER_ID_MISMATCH':
                raise
        except requests.RequestException:
            pass
    current = source_url if eth_item_id(source_url) else 'https://doi.org/' + quote(doi, safe='/')
    for _ in range(6):
        validate_pdf_url(current)
        with requests.get(current, stream=True, timeout=(10, 30), allow_redirects=False,
                          headers={'Accept': 'text/html,application/pdf;q=0.9',
                                   'User-Agent': 'ResearchPilot/0.1 (DOI fulltext lookup)'}) as response:
            if response.is_redirect:
                location = response.headers.get('Location')
                if not location:
                    raise FulltextError('NO_DOI_PDF', 'DOI 原站未提供有效跳转地址。')
                current = urljoin(current, location)
                continue
            if response.status_code >= 400 and eth_item_id(current):
                return find_eth_pdf_urls(current, doi)
            if response.status_code == 404:
                raise FulltextError('NO_DOI_PDF', 'DOI 或原站页面不存在，无法取得全文地址。')
            response.raise_for_status()
            if 'application/pdf' in response.headers.get('Content-Type', '').lower():
                return [current]
            content = bytearray()
            for block in response.iter_content(chunk_size=65536):
                content.extend(block)
                if content.startswith(b'%PDF-'):
                    return [current]
                if len(content) > MAX_HTML_BYTES:
                    raise FulltextError('NO_DOI_PDF', '原站页面过大，未自动解析全文链接。')
        parser = PdfMetadata()
        parser.feed(bytes(content).decode('utf-8', errors='replace'))
        urls = parser.pdf_urls(current)
        if urls:
            return urls[:5]
        if eth_item_id(current):
            return find_eth_pdf_urls(current, doi)
        raise FulltextError('NO_DOI_PDF', 'DOI 原站页面没有提供可识别的 PDF 元数据或下载按钮链接，可能需要登录、动态加载或没有公开全文。')
    raise FulltextError('NO_DOI_PDF', 'DOI 原站跳转次数过多，无法自动取得全文。')
