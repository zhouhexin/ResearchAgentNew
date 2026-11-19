"""arXiv 论文检索，支持年份、排序和分页。"""
import re
import xml.etree.ElementTree as ET

from models import Paper
from tools.arxiv_pdf import ARXIV_ID, NS, arxiv_id, query_arxiv
from tools.open_access_pdf import FulltextError
from storage.repository import normalize_doi

OPEN_SEARCH = '{http://a9.com/-/spec/opensearch/1.1/}'


def search_arxiv_page(query, page=1, page_size=20, start_year=None, end_year=None, sort='relevance', *, deadline=None):
    if not query.strip():
        raise ValueError('检索关键词不能为空')
    if page < 1 or not 1 <= page_size <= 100 or page * page_size > 30000:
        raise ValueError('分页范围无效，arXiv 最多展示前 30000 条结果')
    if start_year and end_year and start_year > end_year:
        raise ValueError('起始年份不能晚于结束年份')
    sort_fields = {'relevance': 'relevance', 'newest': 'submittedDate', 'updated': 'lastUpdatedDate'}
    if sort not in sort_fields:
        raise ValueError('排序方式必须为 relevance、newest 或 updated')
    text = query.strip()
    identifier = arxiv_id(text) or (text if ARXIV_ID.fullmatch(text) else None)
    if identifier:
        expression = 'id:' + identifier
    elif re.search(r'\b(?:all|ti|au|abs|cat|id):', text):
        expression = text
    else:
        words = re.findall(r'"[^"]+"|\S+', text)
        expression = ' AND '.join('all:"' + word.strip('"').replace('\\', '').replace('"', '') + '"' for word in words)
    if start_year or end_year:
        expression = f'({expression}) AND submittedDate:[{start_year or 1991}01010000 TO {end_year or 2100}12312359]'
    payload = query_arxiv({'search_query': expression, 'start': (page - 1) * page_size,
                           'max_results': page_size, 'sortBy': sort_fields[sort], 'sortOrder': 'descending'},
                          **({'deadline': deadline} if deadline is not None else {}))
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise FulltextError('ARXIV_RESPONSE_ERROR', 'arXiv 未返回有效的论文数据，请稍后重试。') from None
    if root.tag != '{http://www.w3.org/2005/Atom}feed':
        raise FulltextError('ARXIV_RESPONSE_ERROR', 'arXiv 返回的内容不是论文检索结果，请稍后重试。')
    items = []
    for entry in root.findall('atom:entry', NS):
        raw_id = entry.findtext('atom:id', default='', namespaces=NS)
        versioned = arxiv_id(raw_id)
        if versioned is None:
            if '/api/errors' in raw_id:
                raise FulltextError('ARXIV_SEARCH_ERROR', 'arXiv 无法处理当前检索表达式，请调整关键词。')
            continue
        base_id = re.sub(r'v\d+$', '', versioned)
        published = entry.findtext('atom:published', default='', namespaces=NS)
        title = ' '.join(entry.findtext('atom:title', default='', namespaces=NS).split())
        authors = [' '.join(author.findtext('atom:name', default='', namespaces=NS).split()) for author in entry.findall('atom:author', NS)]
        # arXiv 的标准 DOI 由基础编号构成，版本更新不会改变收藏标识。
        doi = '10.48550/arxiv.' + base_id.lower()
        items.append({
            'source': 'arxiv', 'title': title, 'authors': [a for a in authors if a],
            'abstract': entry.findtext('atom:summary', default='', namespaces=NS).strip(),
            'year': int(published[:4]) if published[:4].isdigit() else None,
            'doi': doi, 'published_doi': normalize_doi(entry.findtext('arxiv:doi', default='', namespaces=NS)),
            'openalex_id': None, 'arxiv_id': base_id, 'arxiv_version': versioned,
            'url': 'https://arxiv.org/abs/' + base_id,
            'pdf_url': 'https://arxiv.org/pdf/' + versioned,
            'citation_count': None, 'identity_verified': bool(title), 'saved_paper_id': None,
        })
    total_text = root.findtext(OPEN_SEARCH + 'totalResults', default=str(len(items)))
    try:
        total = min(int(total_text), 30000)
    except ValueError:
        raise FulltextError('ARXIV_RESPONSE_ERROR', 'arXiv 返回的分页数据无效。') from None
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size}


def search_arxiv(query, limit=20, start_year=None, end_year=None, sort='relevance'):
    page = search_arxiv_page(query, page_size=limit, start_year=start_year, end_year=end_year, sort=sort)
    return [Paper(title=p['title'], authors=p['authors'], abstract=p['abstract'], year=p['year'],
                  doi=p['doi'], url=p['url'], citation_count=0) for p in page['items']]
