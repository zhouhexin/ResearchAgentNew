"""读取 ETH Research Collection 条目的 ORIGINAL 文件列表。"""
import json
import re
from urllib.parse import urljoin, urlsplit

import requests

from storage.repository import normalize_doi
from tools.open_access_pdf import FulltextError, validate_pdf_url

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
HOSTS = {'www.research-collection.ethz.ch', 'research-collection.ethz.ch'}


def eth_item_id(url):
    parsed = urlsplit(url or '')
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    match = re.fullmatch(r'/(?:entities/publication|items)/(' + UUID + r')/?', parsed.path, re.I)
    return match.group(1) if match else None


def read_json(url):
    parsed = urlsplit(url)
    if parsed.hostname not in HOSTS or not parsed.path.startswith('/server/api/core/'):
        raise FulltextError('NO_DOI_PDF', '机构仓储返回了非预期的文件接口。')
    validate_pdf_url(url)
    with requests.get(url, timeout=(10, 30), stream=True, allow_redirects=False,
                      headers={'Accept': 'application/json', 'User-Agent': 'ResearchPilot/0.1 (repository fulltext lookup)'}) as response:
        response.raise_for_status()
        if response.is_redirect:
            raise FulltextError('NO_DOI_PDF', '机构仓储文件接口发生跳转，未自动读取。')
        data = bytearray()
        for block in response.iter_content(chunk_size=65536):
            data.extend(block)
            if len(data) > 2 * 1024 * 1024:
                raise FulltextError('NO_DOI_PDF', '机构仓储文件列表过大。')
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise FulltextError('NO_DOI_PDF', '机构仓储未返回可识别的文件列表。') from None


def collection(url, name):
    # 常规条目通常只需一页；限制页数，避免无界遍历。
    for _ in range(3):
        data = read_json(url)
        yield from data.get('_embedded', {}).get(name, [])
        following = (data.get('_links', {}).get('next') or {}).get('href')
        if not following:
            break
        url = urljoin(url, following)


def find_eth_pdf_urls(page_url, doi):
    item_id = eth_item_id(page_url)
    if item_id is None:
        return []
    parsed = urlsplit(page_url)
    origin = f'https://{parsed.hostname}'
    item_url = f'{origin}/server/api/core/items/{item_id}'
    item = read_json(item_url)
    if item.get('uuid', item.get('id')) != item_id:
        raise FulltextError('PAPER_ID_MISMATCH', '机构仓储返回的条目编号与论文页面不同，已停止下载。')
    metadata = item.get('metadata') or {}
    identifiers = [record.get('value', '') for key in ('dc.identifier.doi', 'dc.identifier.uri') for record in metadata.get(key, [])]
    if not doi or normalize_doi(doi) not in {normalize_doi(value) for value in identifiers}:
        raise FulltextError('PAPER_ID_MISMATCH', '机构仓储条目的 DOI 与当前论文不同或无法核实，已停止下载。')
    urls = []
    for bundle in collection(item_url + '/bundles?size=100', 'bundles'):
        if bundle.get('name') != 'ORIGINAL':
            continue
        bundle_id = bundle.get('uuid') or bundle.get('id') or ''
        if not re.fullmatch(UUID, bundle_id, re.I):
            continue
        for stream in collection(f'{origin}/server/api/core/bundles/{bundle_id}/bitstreams?size=100', 'bitstreams'):
            name = stream.get('name', '')
            if not name.lower().endswith('.pdf') or re.search(r'supplement|supporting|appendix|license|readme|thumbnail', name, re.I):
                continue
            stream_id = stream.get('uuid') or stream.get('id') or ''
            if not re.fullmatch(UUID, stream_id, re.I):
                continue
            url = f'{origin}/server/api/core/bitstreams/{stream_id}/content'
            if url not in urls:
                urls.append(url)
    if not urls:
        raise FulltextError('NO_DOI_PDF', '机构仓储条目没有可读取的原始 PDF 文件，可能只有元数据或全文尚未开放。')
    return urls[:5]
