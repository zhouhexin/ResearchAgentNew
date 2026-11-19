"""OpenAlex 发现会议候选，各会议官方论文集精确核验。"""
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

import requests
from tools.execution_config import openalex_key, source_ids

from storage.repository import normalize_doi, normalize_openalex_id
from tools.academic_search import restore_abstract
from tools.arxiv_pdf import normalize_title
from tools.open_access_pdf import FulltextError

VERSION = 'cvpr-openalex-cvf-v1'
from tools.venue_search.catalog import CVPR, ICCV, VENUES, SOURCE_QUERIES, VERIFICATION_PROVIDERS, JOURNAL_ISSNS, validate_venues
from tools.venue_search.proceedings import official_location, VERSION as PROCEEDINGS_VERSION
from tools.venue_search.extended import HOSTS as EXTENDED_HOSTS, SLUGS as EXTENDED_SLUGS, official_url_matches, verify_extended
VENUE_SEARCH_VERSION = 'cvf-venue-search-v2'
CACHE_PATH = Path(__file__).resolve().parents[2] / 'outputs' / 'venue_cache.sqlite3'
_LOCK = threading.Lock()
_LAST = {}


def cached_get(url, params=None, *, ttl=86400):
    """只允许固定学术来源，缓存成功响应；不缓存拦截页，不保存密钥。"""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'api.openalex.org', 'openaccess.thecvf.com','ojs.aaai.org','ijcai.org',*EXTENDED_HOSTS} or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise FulltextError('VENUE_URL_INVALID', '发表核验来源地址无效。')
    safe_params = dict(params or {})
    key = json.dumps([VERSION, url, safe_params], sort_keys=True)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with sqlite3.connect(CACHE_PATH, timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, saved REAL NOT NULL, body TEXT NOT NULL)')
            row = db.execute('SELECT saved,body FROM responses WHERE key=?', (key,)).fetchone()
            if row and time.time()-row[0] < ttl:
                return row[1]
        pause = max(0, 1.0 - (time.monotonic()-_LAST.get(parsed.hostname, 0)))
        if pause:
            time.sleep(pause)
        if parsed.hostname == 'api.openalex.org' and (key_value := openalex_key()):
            safe_params['api_key'] = key_value
        try:
            with requests.get(url, params=safe_params, timeout=(10, 30), stream=True, allow_redirects=False,
                              headers={'User-Agent': 'ResearchPilot/0.1 (official proceedings lookup)'}) as response:
                if response.is_redirect:
                    raise FulltextError('VENUE_REDIRECT', '学术来源返回跳转，未继续核验，请稍后重试。')
                response.raise_for_status()
                blocks, size = [], 0
                for block in response.iter_content(65536):
                    size += len(block)
                    if size > 12 * 1024 * 1024:
                        raise FulltextError('VENUE_RESPONSE_TOO_LARGE', '学术来源响应超出大小限制。')
                    blocks.append(block)
                body = b''.join(blocks).decode('utf-8', errors='replace')
                if parsed.hostname in {'api.openalex.org','api.crossref.org','api.openreview.net','api2.openreview.net'}:
                    try:
                        payload = json.loads(body)
                    except ValueError:
                        raise FulltextError('OPENALEX_RESPONSE_ERROR', 'OpenAlex 返回的内容不是有效 JSON。') from None
                    if not isinstance(payload, dict) or 'error' in payload or 'errors' in payload:
                        raise FulltextError('OPENALEX_RESPONSE_ERROR', 'OpenAlex 返回错误响应，未缓存。')
                    if parsed.hostname=='api.crossref.org' and (payload.get('status')!='ok' or not isinstance(payload.get('message'),dict)):
                        raise FulltextError('CROSSREF_RESPONSE_ERROR','出版记录格式无效，未缓存。')
                    if parsed.hostname in {'api.openreview.net','api2.openreview.net'} and not isinstance(payload.get('notes'),list):
                        raise FulltextError('OPENREVIEW_RESPONSE_ERROR','OpenReview 记录格式无效，未缓存。')
                elif parsed.hostname=='openaccess.thecvf.com' and not re.search(r'citation_title|papertitle|ptitle', body, re.I):
                    raise FulltextError('CVF_RESPONSE_ERROR', 'CVF 未返回可识别的论文目录或论文页面，未缓存。')
                elif parsed.hostname in {'ojs.aaai.org','ijcai.org'} and not re.search(r'citation_title|@inproceedings|<h[123][^>]*>.*?(?:proceedings|conference)',body,re.I|re.S):
                    raise FulltextError('PROCEEDINGS_RESPONSE_ERROR','官方论文集未返回可识别的论文内容（可能访问受限），未缓存。')
                elif parsed.hostname in EXTENDED_HOSTS and not re.search(r'citation_title|papertitle|@inproceedings',body,re.I):
                    raise FulltextError('PROCEEDINGS_RESPONSE_ERROR','正式来源未返回可识别的论文内容（可能访问受限），未缓存。')
        finally:
            _LAST[parsed.hostname] = time.monotonic()
        with sqlite3.connect(CACHE_PATH, timeout=10) as db:
            db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?)', (key, time.time(), body))
        return body


def get_json(path, params):
    try:
        result = json.loads(cached_get('https://api.openalex.org/'+path, params))
        if not isinstance(result, dict):
            raise ValueError()
        if path in {'sources', 'works'} and (not isinstance(result.get('results'), list) or
                any(not isinstance(item, dict) for item in result['results'])):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise FulltextError('OPENALEX_RESPONSE_ERROR', 'OpenAlex 返回的数据格式无效。') from None


def resolve_sources(venue_slug='cvpr'):
    validate_venues([venue_slug])
    acronym = VENUES[venue_slug]['acronym']
    env_name = f'OPENALEX_{acronym.upper()}_SOURCE_IDS'
    # 不内置猜测的 S 编号。配置 ID 也必须从 OpenAlex 读取身份后核对。
    configured = [v.strip() for v in source_ids(env_name).split(',') if v.strip()]
    if configured and (len(configured) > 100 or any(not re.fullmatch(r'S\d+', value) for value in configured)):
        raise FulltextError(f'{acronym}_MAPPING_INVALID', f'{env_name} 应为逗号分隔的 OpenAlex S 编号，最多100个。')
    results = [get_json('sources/'+value, {}) for value in configured] if configured else get_json('sources', {
        'search': SOURCE_QUERIES[venue_slug], 'per_page': 100,
        'select': 'id,display_name,type,issn' if venue_slug in JOURNAL_ISSNS else 'id,display_name,type'}).get('results', [])
    matched = {}
    for source in results:
        name = (source.get('display_name') or '').casefold()
        identifier = (source.get('id') or '').rsplit('/', 1)[-1]
        allowed_types={'conference','journal'} if venue_slug=='aaai' else {'conference'}
        name_matches = (name.strip() == 'aaai' or re.search(r'\baaai conference on artificial intelligence\s*(?:\(aaai[\s-]*\d*\))?\s*$', name.strip().rstrip('.'))) if venue_slug == 'aaai' else (
            SOURCE_QUERIES[venue_slug] in name or re.search(r'\b'+venue_slug+r'\b', name))
        if venue_slug in JOURNAL_ISSNS:
            allowed_types={'journal'}
            name_matches=(normalize_title(name) in {normalize_title(VENUES[venue_slug]['name']),normalize_title(VENUES[venue_slug]['name']+' (TOIS)')}
                          and bool(set(source.get('issn') or []) & JOURNAL_ISSNS[venue_slug]))
        elif venue_slug in EXTENDED_SLUGS:
            allowed_types={'conference','journal','book series'}
            name_matches=SOURCE_QUERIES[venue_slug].casefold() in name or name.strip() in {venue_slug,'nips' if venue_slug=='neurips' else venue_slug}
            if venue_slug=='sigir':name_matches=bool(re.search(r'\bsigir\b',name) and re.search(r'research (?:and|&) development (?:in|on) information retrieval',name)) or name.strip()=='sigir'
        if (re.fullmatch(r'S\d+', identifier) and source.get('type') in allowed_types
                and name_matches
                and not re.search(r'workshop|\b(?:cvpr|iccv)w\b|findings', name)):
            matched[identifier] = {'id': identifier, 'name': source['display_name']}
    if not matched:
        raise FulltextError(f'{acronym}_MAPPING_UNAVAILABLE', f'OpenAlex 未返回可确认的 {acronym} 来源，请稍后重试或配置已核实的 {env_name}。')
    if len(matched) > 100 or (configured and set(configured) != set(matched)):
        raise FulltextError(f'{acronym}_MAPPING_AMBIGUOUS', f'{acronym} 来源映射过多或身份不符，需核实 OpenAlex 来源编号。')
    return list(matched.values())


def cvf_publication_location(url):
    """从官方路径读取会议、年份、主会/Workshop，拒绝其他站点和未知 Track。"""
    parsed = urlsplit(url or '')
    if parsed.scheme != 'https' or parsed.netloc != 'openaccess.thecvf.com' or parsed.query or parsed.fragment:
        return None
    match = re.fullmatch(r'/content/(CVPR|ICCV)(\d{4})(W)?/(?:([^/]+)/)?html/[^/]+_paper\.html', parsed.path, re.I)
    if match:
        if match[4] and not match[3]:
            return None
        track = 'findings' if (match[4] or '').casefold() == 'findings' else 'workshop' if match[3] else 'main'
        return match[1].lower(), int(match[2]), track
    match = re.fullmatch(r'/content_(CVPR|ICCV)_(\d{4})(_workshops)?/(?:[^/]+/)?html/[^/]+\.html', parsed.path, re.I)
    if match:
        return match[1].lower(), int(match[2]), 'workshop' if match[3] else 'main'
    return None


def cvf_location(url):
    """保留旧 CVPR 专用函数的语义。"""
    value = cvf_publication_location(url)
    return value[1:] if value and value[0] == 'cvpr' else None


class CVFPage(HTMLParser):
    VOID = {'meta', 'link', 'br', 'img', 'input', 'hr', 'base', 'source', 'wbr', 'area', 'embed', 'param', 'col'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.sections, self.links, self.stack = {}, {}, [], []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.meta.setdefault((attrs.get('name') or '').casefold(), []).append(attrs.get('content', ''))
        if tag not in self.VOID:
            self.stack.append((tag, attrs.get('id')))
        if tag == 'a':
            self.anchor = {'href': attrs.get('href', ''), 'text': []}

    def handle_data(self, value):
        if self.anchor is not None:
            self.anchor['text'].append(value)
        for _, identifier in self.stack:
            if identifier in {'papertitle', 'authors', 'abstract'}:
                self.sections.setdefault(identifier, []).append(value)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.links.append((self.anchor['href'], ' '.join(self.anchor['text']).strip()))
            self.anchor = None
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def section(self, key):
        return ' '.join(' '.join(self.sections.get(key, [])).split())


def verify_publication(paper, venue_slug):
    validate_venues([venue_slug])
    if venue_slug in EXTENDED_SLUGS:
        return verify_extended(paper,venue_slug,cached_get)
    if venue_slug in {'aaai','ijcai'}:
        from tools.venue_search.proceedings import verify_proceedings
        return verify_proceedings(paper,venue_slug,cached_get)
    acronym = VENUES[venue_slug]['acronym']
    year = paper.get('year')
    result = {'track': 'unknown', 'verification_status': 'indexed_unverified', 'verification_url': None,
              'verified_at': None, 'verification_provider':'CVF', 'reason': '仅有 OpenAlex 索引记录，尚未完成官方身份核验。'}
    if not isinstance(year, int) or not 2013 <= year <= date.today().year:
        return result
    try:
        candidates = [url.replace('http://openaccess.thecvf.com/', 'https://openaccess.thecvf.com/', 1) if isinstance(url, str) else url
                      for url in [paper.get('url'), *paper.get('official_urls', [])]]
        direct = [url for url in candidates if cvf_publication_location(url) and cvf_publication_location(url)[0] == venue_slug]
        urls = list(dict.fromkeys(url for url in direct if cvf_publication_location(url)[1] == year))
        if not urls:
            index_url = f'https://openaccess.thecvf.com/{acronym}{year}?day=all'
            index = CVFPage();index.feed(cached_get(index_url, ttl=86400))
            urls = list(dict.fromkeys(urljoin(index_url, link) for link, title in index.links
                if normalize_title(title) == normalize_title(paper.get('title'))
                and cvf_publication_location(urljoin(index_url, link)) == (venue_slug, year, 'main')))
        if len(urls) != 1:
            result['reason'] = '未找到唯一的 CVF 官方论文页面，保持待核验。'
            return result
        url = urls[0]
        page = CVFPage();page.feed(cached_get(url, ttl=7*86400))
        title = next(iter(page.meta.get('citation_title', [])), '') or page.section('papertitle')
        authors = page.meta.get('citation_author') or re.split(r',|;', page.section('authors'))
        def person(value):
            return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isalnum())
        expected_authors = {person(author) for author in paper.get('authors', [])} - {''}
        official_authors = {person(author) for author in authors} - {''}
        official_doi = next(iter(page.meta.get('citation_doi', [])), '')
        if (not normalize_title(title) or normalize_title(title) != normalize_title(paper.get('title'))
                or not expected_authors or not official_authors or not expected_authors <= official_authors
                or (official_doi and paper.get('doi') and normalize_doi(official_doi) != normalize_doi(paper['doi']))):
            result['reason'] = 'CVF 页面标题、作者或 DOI 与候选不一致，未确认发表身份。'
            return result
        track = cvf_publication_location(url)[2]
        pdfs = [urljoin(url, value) for value in page.meta.get('citation_pdf_url', [])]
        pdfs += [urljoin(url, link) for link, _ in page.links if link.endswith('.pdf') and '/supplemental/' not in link]
        pdfs = [p for p in pdfs if urlsplit(p).netloc == 'openaccess.thecvf.com' and urlsplit(p).scheme == 'https']
        result.update(track=track, verification_status='official_verified', verification_url=url,
                      verified_at=datetime.now(timezone.utc).isoformat(), reason='已核对 CVF 官方页面的标题、作者及发表路径。',
                      abstract=page.section('abstract'), pdf_url=next(iter(pdfs), None))
        return result
    except (requests.RequestException, FulltextError):
        result['reason'] = 'CVF 官方来源暂时不可用，未确认发表身份；可稍后重新检索。'
        return result


def verify_cvpr(paper):
    return verify_publication(paper, 'cvpr')


def search_venue_page(query, *, venue_slugs, page=1, page_size=10, start_year=None, end_year=None, sort='relevance', verified_main_only=False):
    venue_slugs = validate_venues(venue_slugs)
    if not query.strip() or not 1 <= page_size <= 20 or page < 1 or page*page_size > 10000:
        raise ValueError('会议检索关键词或分页范围无效；每页最多20篇，最多前10000条候选。')
    if start_year and end_year and start_year > end_year:
        raise ValueError('起始年份不能晚于截止年份。')
    if sort not in {'relevance', 'newest', 'cited'}:
        raise ValueError('会议检索排序仅支持 relevance、newest 或 cited。')
    sources, source_venues = [], {}
    for slug in venue_slugs:
        for source in (resolve_sources() if slug == 'cvpr' else resolve_sources(slug)):
            if source['id'] in source_venues and source_venues[source['id']] != slug:
                raise FulltextError('VENUE_MAPPING_AMBIGUOUS', '同一来源编号映射到了不同会议，已停止检索，请核实来源配置。')
            source_venues[source['id']] = slug
            sources.append(dict(source, venue_slug=slug))
    if len(sources)>100:
        raise FulltextError('VENUE_MAPPING_TOO_MANY','所选会议来源合计超过100个，需配置已核实的来源编号后重试。')
    filters = ['primary_location.source.id:'+'|'.join(source['id'] for source in sources)]
    if start_year:
        filters.append('from_publication_date:'+str(start_year)+'-01-01')
    if end_year:
        filters.append('to_publication_date:'+str(end_year)+'-12-31')
    params = {'search': query.strip(), 'filter': ','.join(filters), 'page': page, 'per_page': page_size,
              'select': 'id,title,doi,publication_year,authorships,abstract_inverted_index,cited_by_count,primary_location,locations'}
    if sort != 'relevance':
        params['sort'] = 'publication_date:desc' if sort == 'newest' else 'cited_by_count:desc'
    data = get_json('works', params)
    deadline = time.monotonic()+90
    items, skipped, duplicates, seen = [], 0, 0, set()
    for work in data.get('results', []):
        try:
            identifier = normalize_openalex_id(work.get('id'))
        except ValueError:
            identifier = None
        if not identifier or not (work.get('title') or '').strip():
            skipped += 1
            continue
        locations = [work.get('primary_location') or {}, *(work.get('locations') or [])]
        source_id = ((locations[0].get('source') or {}).get('id') or '').rsplit('/', 1)[-1]
        if source_id not in {source['id'] for source in sources}:
            skipped += 1
            continue
        slug = source_venues[source_id]
        urls = [location.get('landing_page_url') for location in locations if location.get('landing_page_url')]
        doi = normalize_doi(work.get('doi'))
        dedup_key = (slug, doi or identifier)
        if dedup_key in seen:
            duplicates += 1
            continue
        seen.add(dedup_key)
        item = {'source': 'openalex', 'venue_slug': slug, 'venue': dict(VENUES[slug]), 'arxiv_id': None,
                'title': work['title'].strip(), 'authors': [a['author']['display_name'] for a in work.get('authorships', []) if (a.get('author') or {}).get('display_name')],
                'abstract': restore_abstract(work.get('abstract_inverted_index')), 'year': work.get('publication_year'),
                'abstract_source': 'openalex',
                'doi': doi, 'published_doi': doi, 'openalex_id': identifier,
                'url': next(iter(urls), None) or ('https://doi.org/'+doi if doi else identifier),
                'citation_count': work.get('cited_by_count'), 'identity_verified': True, 'saved_paper_id': None,
                'official_urls': [url for url in urls if
                    (cvf_publication_location(url) and cvf_publication_location(url)[0] == slug) or
                    (official_location(url) and official_location(url)['venue_slug'] == slug) or official_url_matches(url,slug)]}
        publication = (verify_cvpr(item) if slug == 'cvpr' else verify_publication(item, slug)) if time.monotonic() < deadline else {
            'track': 'unknown', 'verification_status': 'indexed_unverified', 'verification_url': None,
            'verified_at': None, 'verification_provider':VERIFICATION_PROVIDERS[slug], 'reason': '本轮官方核验等待时间已达上限，保持待核验；再次检索可复用已缓存页面继续核对。'}
        item['publication'] = {k: v for k, v in publication.items() if k not in {'abstract', 'pdf_url','paper_url'}}
        if publication['verification_status'] == 'official_verified':
            item['url'] = publication.get('paper_url') or publication['verification_url']
            item['pdf_url'] = publication.get('pdf_url')
            if publication.get('abstract'):
                item['abstract'] = publication['abstract']
                item['abstract_source'] = 'cvf' if slug in {'cvpr','iccv'} else slug+'_proceedings'
        else:
            item['abstract_source'] = 'openalex'
        items.append(item)
    counts = {'main': sum(p['publication']['verification_status'] == 'official_verified' and p['publication']['track'] == 'main' for p in items),
              'workshop': sum(p['publication']['track'] == 'workshop' for p in items),
              'findings': sum(p['publication']['track'] == 'findings' for p in items),
              'other': sum(p['publication']['track'] == 'other' for p in items),
              'journal': sum(p['publication']['verification_status']=='official_verified' and p['publication']['track']=='journal' for p in items),
              'unverified': sum(p['publication']['verification_status'] != 'official_verified' for p in items)}
    if verified_main_only:
        items = [item for item in items if item['publication']['verification_status'] == 'official_verified' and item['publication']['track'] in {'main','journal'}]
    return {'items': items, 'total': int(data.get('meta', {}).get('count', len(items))), 'page': page, 'page_size': page_size,
            'source_ids': sources, 'skipped_count': skipped, 'duplicate_count': duplicates, 'publication_counts': counts, 'venues': venue_slugs,
            'notice': 'OpenAlex 返回 '+ '、'.join(VENUES[v]['acronym'] for v in venue_slugs)+' 来源候选；逐篇以对应官方论文集核验。官方访问受限时保留待核验。总数为索引候选数，主会过滤仅作用于当前页，未覆盖全部论文。'}


def search_cvpr_page(query, **kwargs):
    return search_venue_page(query, venue_slugs=['cvpr'], **kwargs)


def search_iccv_page(query, **kwargs):
    return search_venue_page(query, venue_slugs=['iccv'], **kwargs)


def find_cvf_pdf_urls(title, authors, doi, source_url):
    location = cvf_publication_location(source_url)
    if location is None:
        raise FulltextError('NO_CVF_PDF', '当前论文没有明确的 CVF 官方论文页面。')
    publication = verify_publication({'title': title, 'authors': authors, 'doi': doi, 'year': location[1], 'url': source_url}, location[0])
    if publication['verification_status'] != 'official_verified' or not publication.get('pdf_url'):
        raise FulltextError('NO_CVF_PDF', 'CVF 官方页面尚未确认身份或未提供 PDF 地址，可尝试其他来源或手动上传。')
    return [publication['pdf_url']]


def find_cvpr_pdf_urls(title, authors, doi, source_url):
    if cvf_location(source_url) is None:
        raise FulltextError('NO_CVF_PDF', '当前论文没有明确的 CVPR 官方论文页面。')
    return find_cvf_pdf_urls(title, authors, doi, source_url)
