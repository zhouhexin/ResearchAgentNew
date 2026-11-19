"""arXiv Atom API 查询：明确编号，或严格核对标题和作者后下载。"""
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

import requests

from storage.repository import normalize_doi
from tools.open_access_pdf import FulltextError

ARXIV_API = 'https://export.arxiv.org/api/query'
NS = {'atom': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}
ARXIV_ID = re.compile(r'(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(?:v[1-9]\d*)?', re.I)
_QUERY_LOCK = threading.Lock()
_last_query = 0.0


def arxiv_id(value):
    raw = unquote((value or '').strip())
    if raw.lower().startswith('10.48550/arxiv.'):
        candidate = raw[len('10.48550/arxiv.'):]
    else:
        parsed = urlsplit(raw)
        if parsed.hostname not in {'arxiv.org', 'export.arxiv.org', 'www.arxiv.org'}:
            return None
        candidate = re.sub(r'^/(?:abs|pdf)/', '', parsed.path).removesuffix('.pdf')
    return candidate if ARXIV_ID.fullmatch(candidate) else None


def normalize_title(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value or '').casefold() if c.isalnum())


def author_keys(value):
    text = unicodedata.normalize('NFKD', value or '').casefold()
    text = ''.join(c for c in text if not unicodedata.combining(c))
    if ',' in text:
        text = text.split(',', 1)[0]
    else:
        text = text.split()[-1] if text.split() else ''
    return ''.join(c for c in text if c.isalnum())


def query_arxiv(params, *, deadline=None):
    global _last_query
    # arXiv 要求请求之间留出间隔；所有本地查询共享节流锁。
    with _QUERY_LOCK:
        pause = max(0.0, 3.0 - (time.monotonic() - _last_query))
        if pause:
            time.sleep(pause)
        try:
            timeout = (10, 30)
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise requests.Timeout("arXiv request deadline exceeded before dispatch")
                timeout = (min(10, remaining), min(30, remaining))
            with requests.get(ARXIV_API, params=params, timeout=timeout,
                              headers={'User-Agent': 'ResearchPilot/0.1 (arXiv metadata lookup)'}) as response:
                response.raise_for_status()
                return response.content
        finally:
            _last_query = time.monotonic()


def find_arxiv_pdf_urls(title='', authors=None, doi=None, source_url=''):
    expected_doi = normalize_doi(doi)
    explicit_id = arxiv_id(source_url) or arxiv_id(expected_doi)
    expected_authors = {author_keys(author) for author in authors or []} - {''}
    if explicit_id:
        params = {'id_list': explicit_id, 'max_results': 1}
    elif title.strip() and expected_authors:
        # 只查询标题；下面仍要精确核对标题与作者，不直接选择搜索的第一项。
        clean_title = re.sub(r'["\\\r\n]', ' ', title).strip()
        params = {'search_query': f'ti:"{clean_title}"', 'start': 0, 'max_results': 5}
    else:
        raise FulltextError('NO_ARXIV_MATCH', '缺少明确的 arXiv 编号或完整标题、作者，无法安全匹配 arXiv 论文。')
    try:
        root = ET.fromstring(query_arxiv(params))
    except ET.ParseError:
        raise FulltextError('ARXIV_RESPONSE_ERROR', 'arXiv 未返回有效的论文数据，请稍后重试。') from None
    urls = []
    entries = root.findall('atom:entry', NS)
    rejected = {'title': 0, 'author': 0, 'doi': 0, 'id': 0}
    for entry in entries:
        entry_id = arxiv_id(entry.findtext('atom:id', default='', namespaces=NS))
        if entry_id is None:
            rejected['id'] += 1
            continue
        entry_doi = normalize_doi(entry.findtext('arxiv:doi', default='', namespaces=NS))
        if expected_doi and not arxiv_id(expected_doi) and entry_doi and entry_doi != expected_doi:
            rejected['doi'] += 1
            continue
        if explicit_id:
            if re.sub(r'v\d+$', '', explicit_id) != re.sub(r'v\d+$', '', entry_id):
                rejected['id'] += 1
                continue
            # 明确要求的版本不能默默替换成其他版本。
            if re.search(r'v\d+$', explicit_id) and explicit_id != entry_id:
                rejected['id'] += 1
                continue
        else:
            entry_title = entry.findtext('atom:title', default='', namespaces=NS)
            names = [a.findtext('atom:name', default='', namespaces=NS) for a in entry.findall('atom:author', NS)]
            if normalize_title(entry_title) != normalize_title(title):
                rejected['title'] += 1
                continue
            if not expected_authors.intersection(author_keys(name) for name in names):
                rejected['author'] += 1
                continue
        # 编号来自 arXiv 返回记录，统一构造官方 PDF 地址。
        url = f'https://arxiv.org/pdf/{entry_id}'
        if url not in urls:
            urls.append(url)
    if not urls:
        if not entries:
            message = 'arXiv 本次查询没有返回论文记录，可能未收录或检索标题不同。'
        else:
            reasons = [f'{label} {rejected[key]} 条' for key, label in [('title', '标题不一致'), ('author', '作者不匹配'), ('doi', 'DOI 冲突'), ('id', '编号或版本不匹配')] if rejected[key]]
            message = f'arXiv 返回 {len(entries)} 条记录，但未通过核对：' + '；'.join(reasons) + '。'
        raise FulltextError('NO_ARXIV_MATCH', message)
    return urls
