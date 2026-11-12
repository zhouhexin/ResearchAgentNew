"""ACL/PMLR/ECVA/NeurIPS/OpenReview 与出版方 DOI 记录核验。

索引只用于发现候选。身份、年份和类别必须来自各自的正式记录。
"""
from datetime import date, datetime, timezone
from html import unescape
import json
import re
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import requests

from storage.repository import normalize_doi
from tools.arxiv_pdf import normalize_title
from tools.open_access_pdf import FulltextError
from tools.venue_search.catalog import JOURNAL_ISSNS, VERIFICATION_PROVIDERS
from tools.venue_search.proceedings import ProceedingsPage, person

VERSION = 'extended-official-venues-v1'
SLUGS = {'acl','emnlp','sigir','neurips','artificial-intelligence','tois','eccv','icml','iclr'}
HOSTS = {'aclanthology.org','proceedings.mlr.press','www.ecva.net','ecva.net',
         'papers.nips.cc','proceedings.nips.cc','proceedings.neurips.cc','papers.neurips.cc',
         'proceedings.iclr.cc','api2.openreview.net','api.openreview.net','api.crossref.org'}


def locate(url):
    """只规范化明确的论文页；主机相同不意味着属于所选会议。"""
    try:
        p=urlsplit(url or '')
        if p.scheme not in {'http','https'} or p.username or p.password or p.port not in (None,80,443):return None
    except ValueError:return None
    host=p.hostname
    if host=='aclanthology.org':
        identifier=p.path.strip('/').removesuffix('.pdf')
        m=re.fullmatch(r'(20\d{2})\.(acl(?:-[a-z]+)?|emnlp(?:-[a-z]+)?|findings-(?:acl|emnlp))\.([1-9]\d*)',identifier)
        if m:
            volume=m[2];slug='emnlp' if 'emnlp' in volume else 'acl'
            track='findings' if volume.startswith('findings-') else 'main' if volume in {'acl-long','acl-short','emnlp-main','emnlp-long','emnlp-short','acl','emnlp'} else 'other'
            return dict(kind='acl',slugs={slug},year=int(m[1]),track=track,id=identifier,url=f'https://aclanthology.org/{identifier}/')
        m=re.fullmatch(r'([PD])(\d{2})-([1-9]\d{3})',identifier,re.I)
        if m:
            year=int(m[2]);year+=1900 if year>=60 else 2000
            slug='acl' if m[1].upper()=='P' else 'emnlp'
            identifier=identifier.upper()
            return dict(kind='acl',slugs={slug},year=year,track='main' if m[3][0] in ('1','2') else 'other',id=identifier,url=f'https://aclanthology.org/{identifier}/')
    if host=='proceedings.mlr.press' and (m:=re.fullmatch(r'/(v\d+)/([a-zA-Z0-9_-]+)\.html',p.path)):
        return dict(kind='pmlr',slugs={'icml'},year=None,track='unknown',id=m[2],volume=m[1],url='https://'+host+p.path)
    if host in {'papers.nips.cc','proceedings.nips.cc','proceedings.neurips.cc','papers.neurips.cc','proceedings.iclr.cc'}:
        m=re.fullmatch(r'/(?:paper_files/)?paper/(\d{4})/hash/([a-f\d]{32})-Abstract(?:-([A-Za-z_]+))?\.html',p.path)
        if m:
            slug='iclr' if host=='proceedings.iclr.cc' else 'neurips'
            suffix=m[3] or ''
            track='workshop' if 'workshop' in suffix.casefold() else 'main' if suffix=='Conference' or (not suffix and int(m[1])<=2020 and slug=='neurips') else 'other' if suffix else 'unknown'
            return dict(kind='proceedings',slugs={slug},year=int(m[1]),track=track,id=m[2],url='https://'+host+p.path)
    if host in {'www.ecva.net','ecva.net'}:
        m=re.fullmatch(r'/papers/eccv_(\d{4})/papers_ECCV(W)?/html/([^/]+)_paper\.php',p.path)
        if m:
            return dict(kind='ecva',slugs={'eccv'},year=int(m[1]),track='workshop' if m[2] else 'main',id=m[3],url='https://www.ecva.net'+p.path)
    if host=='openreview.net' and p.path in {'/forum','/pdf'}:
        values=parse_qs(p.query)
        if len(values.get('id',[]))==1 and re.fullmatch(r'[\w-]{6,80}',values['id'][0],re.ASCII):
            identifier=values['id'][0]
            return dict(kind='openreview',slugs={'iclr','neurips','icml','eccv'},year=None,track='unknown',id=identifier,url='https://openreview.net/forum?'+urlencode({'id':identifier}))
    return None


def official_url_matches(url,slug):
    value=locate(url)
    return bool(value and slug in value['slugs'])


def pending(slug,reason='仅有索引记录，尚未核对正式论文集或出版记录。'):
    return dict(track='unknown',verification_status='indexed_unverified',verification_url=None,
                verified_at=None,verification_provider=VERIFICATION_PROVIDERS[slug],reason=reason)


def confirmed(slug,url,track,provider=None,**extra):
    return dict(track=track,verification_status='official_verified',verification_url=url,
                verified_at=datetime.now(timezone.utc).isoformat(),verification_provider=provider or VERIFICATION_PROVIDERS[slug],
                reason='已核对正式记录的标题、作者和年份；类别取自对应论文集、录用字段或出版方 DOI 元数据。',**extra)


def plain(value):
    return ' '.join(re.sub(r'<[^>]*>','',unescape(value if isinstance(value,str) else '')).split())


def identity(paper,title,authors,years,doi=None):
    expected={person(a) for a in paper.get('authors',[]) if isinstance(a,str)}-{''}
    actual={person(a) for a in authors if isinstance(a,str)}-{''}
    candidate_doi=normalize_doi(paper.get('doi'))
    return (bool(expected) and expected<=actual and bool(normalize_title(title))
            and normalize_title(title)==normalize_title(paper.get('title')) and paper.get('year') in years
            and not (candidate_doi and doi and candidate_doi!=normalize_doi(doi)))


class OfficialPage(ProceedingsPage):
    def handle_data(self,value):
        super().handle_data(value)
        for _,attrs in self.stack:
            key=attrs.get('id')
            if key in {'papertitle','authors'}:
                self.sections.setdefault(key,[]).append(value);break

    def section(self,key):
        return ' '.join(' '.join(self.sections.get(key,[])).split())


def own_bib_fields(text,key):
    # 仅解析属于当前 URL 的 BibTeX，拒绝参考文献或页面中的其他论文。
    match=re.search(r'@inproceedings\s*\{\s*'+re.escape(key)+r'\s*,',text,re.I)
    if not match:return {}
    depth=1;end=match.end()
    while end<len(text) and depth:
        if text[end]=='{':depth+=1
        elif text[end]=='}':depth-=1
        end+=1
    if depth:return {}
    return {name:re.sub(r'[{}]','',m[1]).strip() for name in ('title','author','year','booktitle','doi')
            if (m:=re.search(r'\b'+name+r'\s*=\s*\{((?:[^{}]|\{[^{}]*\})*)\}',text[match.end():end-1],re.I|re.S))}


def official_pdf(loc,page):
    options=[*page.meta.get('citation_pdf_url',[]),*(href for href,label,_ in page.links if re.fullmatch(r'\s*(?:\[?pdf\]?|paper|download pdf)\s*',label,re.I))]
    for raw in options:
        url=urljoin(loc['url'],raw);p=urlsplit(url)
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443):continue
        kind=loc['kind'];identifier=loc['id']
        if kind=='acl' and p.hostname=='aclanthology.org' and p.path==f'/{identifier}.pdf':return url
        if kind=='pmlr':
            volume=loc['volume']
            if p.hostname=='proceedings.mlr.press' and p.path==f'/{volume}/{identifier}/{identifier}.pdf':return url
            if p.hostname=='raw.githubusercontent.com' and re.fullmatch(r'/mlresearch/'+volume+r'/(?:main|gh-pages)/assets/'+re.escape(identifier)+r'/'+re.escape(identifier)+r'\.pdf',p.path):return url
        if kind=='proceedings' and p.hostname==urlsplit(loc['url']).hostname and re.fullmatch(r'/(?:paper_files/)?paper/'+str(loc['year'])+r'/file/'+identifier+r'-Paper(?:-[A-Za-z_]+)?\.pdf',p.path):return url
        if kind=='ecva' and p.hostname in {'www.ecva.net','ecva.net'}:
            expected=urlsplit(loc['url']).path.rsplit('/html/',1)[0]+'/papers/'
            if p.path.startswith(expected):
                stem=p.path[len(expected):]
                number=re.match(r'^(\d+)_ECCV_',identifier,re.I)
                if number and re.fullmatch(r'\d+\.pdf',stem) and int(stem[:-4])==int(number[1]):return url
                if stem==identifier+'_paper.pdf':return url
    return None


def verify_html(paper,slug,loc,fetch):
    page=OfficialPage(slug);page.feed(fetch(loc['url'],ttl=7*86400))
    bib=own_bib_fields(' '.join(page.text),'pmlr-'+loc['volume']+'-'+loc['id']) if loc['kind']=='pmlr' else {}
    title=page.first('citation_title','dc.title') or bib.get('title') or page.section('papertitle') or next(iter(page.headings),'')
    authors=page.meta.get('citation_author') or page.meta.get('dc.creator') or (bib['author'].split(' and ') if bib.get('author') else page.section('authors').split(','))
    year_match=re.search(r'\b(?:19|20)\d{2}\b',page.first('citation_publication_date','citation_date','dc.date.issued') or bib.get('year',''))
    years={int(year_match[0])} if year_match else {loc['year']} if loc['year'] else set()
    doi=page.first('citation_doi','dc.identifier.doi')
    if not doi:
        doi=next((href for href,label,_ in page.links if urlsplit(href).hostname=='doi.org' and label.casefold() in {'doi',paper.get('doi','')}),None)
    if not identity(paper,plain(title),authors,years,doi) or (loc['year'] and paper['year']!=loc['year']):
        return pending(slug,'正式论文页的标题、作者、年份或 DOI 与候选不一致，保持待核验。')
    track=loc['track']
    if loc['kind']=='pmlr':
        book=plain(page.first('citation_conference_title','citation_journal_title') or bib.get('booktitle',''))
        if not re.search(r'\binternational conference on machine learning\b',book,re.I):
            return pending(slug,'PMLR 是多会议论文集，当前卷未能确认为 ICML，不能推定为 ICML 主会。')
        track='workshop' if re.search(r'workshop',book,re.I) else 'main'
    return confirmed(slug,loc['url'],track,abstract=page.section('abstract') or page.first('citation_abstract','dc.description'),pdf_url=official_pdf(loc,page))


def _value(content,key,default=None):
    value=content.get(key,default)
    return value.get('value',default) if isinstance(value,dict) else value


def verify_openreview(paper,slug,loc,fetch):
    base={'iclr':'ICLR.cc','neurips':'NeurIPS.cc','icml':'ICML.cc','eccv':'thecvf.com/ECCV'}[slug]
    for api in ('api2.openreview.net','api.openreview.net'):
        try:
            data=json.loads(fetch('https://'+api+'/notes',{'id':loc['id']},ttl=3600))
        except (requests.RequestException,FulltextError,ValueError):continue
        notes=data.get('notes',[]) if isinstance(data,dict) else []
        if not isinstance(notes,list) or len(notes)!=1:continue
        note=notes[0]
        if not isinstance(note,dict) or note.get('id')!=loc['id'] or note.get('forum',loc['id'])!=loc['id']:continue
        content=note.get('content',{})
        if not isinstance(content,dict):continue
        venueid=_value(content,'venueid','');venue=_value(content,'venue','')
        year=paper['year'];authors=_value(content,'authors',[])
        if not isinstance(authors,list) or not identity(paper,_value(content,'title',''),authors,{year}):continue
        # 同时要求正式 venueid 和明确录用展示类别；Submission/Rejected/Withdrawn 都不能通过。
        if venueid!=f'{base}/{year}/Conference' or not isinstance(venue,str):continue
        if not re.fullmatch(re.escape(slug)+r'\s+'+str(year)+r'\s+(?:poster|oral|spotlight|accepted|accept(?:ed)?\s*\((?:poster|oral|spotlight)\))',venue,re.I):continue
        pdf=_value(content,'pdf')
        return confirmed(slug,loc['url'],'main',provider='OpenReview',
                         abstract=plain(_value(content,'abstract','')),pdf_url='https://openreview.net/pdf?'+urlencode({'id':loc['id']}) if pdf else None)
    return pending(slug,'OpenReview 未能同时确认论文身份、所选年份的正式会议 venueid 和录用类别；公开投稿不等于已录用。')


def crossref_years(record):
    years=set()
    for key in ('published','published-print','published-online','issued'):
        value=record.get(key) or {}
        parts=value.get('date-parts',[]) if isinstance(value,dict) else []
        if parts and isinstance(parts[0],list) and parts[0] and isinstance(parts[0][0],int):years.add(parts[0][0])
    return years


def verify_crossref(paper,slug,fetch):
    doi=normalize_doi(paper.get('doi'))
    if not doi:return pending(slug)
    url='https://api.crossref.org/works/'+quote(doi,safe='')
    data=json.loads(fetch(url,ttl=7*86400));record=data.get('message',{}) if isinstance(data,dict) else {}
    if not isinstance(record,dict) or normalize_doi(record.get('DOI'))!=doi:return pending(slug)
    titles=record.get('title') or [];containers=record.get('container-title') or []
    if not isinstance(titles,list) or not isinstance(containers,list):return pending(slug)
    authors=[' '.join(filter(None,[a.get('given'),a.get('family')])) for a in record.get('author',[]) if isinstance(a,dict)]
    if not identity(paper,plain(next(iter(titles),'')),authors,crossref_years(record),doi):return pending(slug,'出版方 DOI 记录的标题、作者或发表年份与候选不一致，保持待核验。')
    container=plain(next(iter(containers),''));normalized=normalize_title(container)
    if slug in JOURNAL_ISSNS:
        names={'artificial-intelligence':{'artificialintelligence'},'tois':{'acmtransactionsoninformationsystems','acmtransactionsoninformationsystemstois'}}[slug]
        if record.get('type')!='journal-article' or normalized not in names or not (set(record.get('ISSN') or [])&JOURNAL_ISSNS[slug]):return pending(slug,'DOI 的期刊名称、文章类型或 ISSN 不属于所选期刊，保持待核验。')
        track='journal'
    elif slug=='sigir':
        if record.get('type')!='proceedings-article' or not re.search(r'\bsigir\b',container,re.I) or not re.search(r'research (?:and|&) development (?:in|on) information retrieval',container,re.I):return pending(slug)
        track='workshop' if re.search(r'workshop',container,re.I) else 'main'
    elif slug=='eccv':
        if record.get('type') not in {'book-chapter','proceedings-article'} or not re.search(r'\bECCV\s*'+str(paper['year'])+r'\b',container,re.I) or not re.search(r'computer vision',container,re.I):return pending(slug)
        track='workshop' if re.search(r'workshop',container,re.I) else 'main'
    else:return pending(slug)
    return confirmed(slug,url,track,provider='Crossref 出版记录',paper_url='https://doi.org/'+doi,
                     abstract=plain(record.get('abstract','')),pdf_url=None)


def verify_extended(paper,slug,fetch):
    if not isinstance(paper.get('year'),int) or not 1970<=paper['year']<=date.today().year:return pending(slug)
    urls=[paper.get('url'),*paper.get('official_urls',[])]
    doi=normalize_doi(paper.get('doi'))
    if slug in {'acl','emnlp'} and doi and re.match(r'^10\.(?:18653|3115)/v1/',doi):
        identifier=doi.split('/v1/',1)[1]
        if re.fullmatch(r'[pd]\d{2}-\d+',identifier):identifier=identifier.upper()
        urls.append('https://aclanthology.org/'+identifier+'/')
    locations={loc['url']:loc for url in urls if (loc:=locate(url)) and slug in loc['slugs']}
    result=pending(slug)
    # 有正式论文集时优先采用；多个冲突论文页不随意挑一个。
    if len(locations)>1:
        html={url:loc for url,loc in locations.items() if loc['kind']!='openreview'}
        locations=html if len(html)==1 else locations
    if len(locations)==1:
        try:
            loc=next(iter(locations.values()))
            result=verify_openreview(paper,slug,loc,fetch) if loc['kind']=='openreview' else verify_html(paper,slug,loc,fetch)
            if result['verification_status']=='official_verified':return result
        except (requests.RequestException,FulltextError,ValueError,TypeError,KeyError):
            result=pending(slug,'正式来源暂不可用、访问受限或格式未能确认，保持待核验。')
    if slug in {'sigir','tois','artificial-intelligence','eccv'}:
        try:
            return verify_crossref(paper,slug,fetch)
        except (requests.RequestException,FulltextError,ValueError,TypeError,KeyError):
            return pending(slug,'出版方 DOI 记录暂不可用或格式未确认，保持待核验；不能据此判断论文未发表。')
    return result


def find_extended_pdf_urls(title,authors,doi,source_url,fetch):
    loc=locate(source_url)
    if not loc:raise FulltextError('NO_OFFICIAL_PDF','没有明确的受支持官方论文页。')
    # 在无年份的 PMLR/OpenReview 页面中读取年份，随后仍按完整身份再次核验。
    year=loc['year'];slugs=loc['slugs']
    if year is None:
        if loc['kind']=='openreview':
            for api in ('api2.openreview.net','api.openreview.net'):
                try:
                    data=json.loads(fetch('https://'+api+'/notes',{'id':loc['id']},ttl=3600))
                    notes=data.get('notes',[])
                    if len(notes)!=1 or notes[0].get('id')!=loc['id']:continue
                    content=notes[0].get('content',{});venueid=_value(content,'venueid','')
                    match=re.fullmatch(r'(ICLR\.cc|NeurIPS\.cc|ICML\.cc|thecvf\.com/ECCV)/(\d{4})/Conference',venueid)
                    if match:
                        slugs={dict(zip(('ICLR.cc','NeurIPS.cc','ICML.cc','thecvf.com/ECCV'),('iclr','neurips','icml','eccv')))[match[1]]};year=int(match[2]);break
                except (requests.RequestException,FulltextError):continue
        else:
            page=OfficialPage('icml');page.feed(fetch(loc['url'],ttl=7*86400))
            bib=own_bib_fields(' '.join(page.text),'pmlr-'+loc['volume']+'-'+loc['id'])
            match=re.search(r'\b(?:19|20)\d{2}\b',page.first('citation_publication_date','citation_date') or bib.get('year',''))
            year=int(match[0]) if match else None
    for slug in sorted(slugs):
        result=verify_extended(dict(title=title,authors=authors,doi=doi,url=source_url,year=year),slug,fetch)
        if result['verification_status']=='official_verified' and result.get('pdf_url'):return [result['pdf_url']]
    raise FulltextError('NO_OFFICIAL_PDF','未能确认正式论文身份、录用状态或可下载的正文 PDF，请尝试其他来源或手动上传。')
