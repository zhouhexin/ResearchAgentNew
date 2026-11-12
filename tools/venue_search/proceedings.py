"""AAAI OJS 与 IJCAI 官方论文页核验，不用索引信息推定主会。"""
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

import requests
from storage.repository import normalize_doi
from tools.arxiv_pdf import normalize_title
from tools.open_access_pdf import FulltextError
from tools.venue_search.catalog import VERIFICATION_PROVIDERS

VERSION = 'aaai-ijcai-proceedings-v1'


def official_location(url):
    try:
        p = urlsplit(url or '')
        valid = p.scheme in {'http', 'https'} and not p.username and not p.password and p.port in (None, 80, 443)
    except ValueError:
        return None
    if not valid:
        return None
    if p.hostname == 'ojs.aaai.org':
        match = re.fullmatch(r'/index\.php/AAAI/article/(?:view|download)/(\d+)(?:/\d+){0,2}/?', p.path)
        if match:
            return {'venue_slug':'aaai', 'year':None, 'article_id':match[1],
                    'url':'https://ojs.aaai.org/index.php/AAAI/article/view/'+match[1]}
    if p.hostname in {'ijcai.org','www.ijcai.org'}:
        match = re.fullmatch(r'/proceedings/(\d{4})/(\d+)(?:\.pdf)?/?', p.path)
        if match:
            return {'venue_slug':'ijcai','year':int(match[1]),'article_id':str(int(match[2])),
                    'url':f'https://ijcai.org/proceedings/{match[1]}/{int(match[2])}'}
    return None


def classify_track(label, venue):
    text = ' '.join(label.casefold().split())
    if re.search(r'workshops?|symposi(?:um|a)', text):return 'workshop'
    if 'findings' in text:return 'findings'
    if re.search(r'special|survey|demo|doctoral|student|senior member|sister conference|journal track|spotlight|iaai|eaai|ai4tech|ai and|human.centred', text):return 'other'
    if re.search(r'\bmain(?: technical)? track\b|\btechnical papers\b', text):return 'main'
    if venue == 'aaai' and re.search(r'\btechnical tracks?\s*\d*\b', text):return 'main'
    return 'unknown'


class ProceedingsPage(HTMLParser):
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','wbr'}
    def __init__(self, venue):
        super().__init__(convert_charrefs=True)
        self.venue, self.meta, self.stack = venue, {}, []
        self.heading, self.headings, self.track = None, [], 'unknown'
        self.abstract_heading = False
        self.anchor, self.links, self.text, self.sections, self.authors = None, [], [], {}, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.meta.setdefault((attrs.get('name') or attrs.get('property') or '').casefold(),[]).append(attrs.get('content',''))
        if tag not in self.VOID:self.stack.append((tag,attrs))
        if tag in {'h1','h2','h3','h4','h5','h6'}:
            self.heading=(tag,[])
            self.abstract_heading=False
        if tag == 'a':self.anchor={'href':attrs.get('href',''),'text':[],'track':self.track}

    def handle_data(self, value):
        # 引用区的内容不能用作本文身份或 Track 的证据。
        if any(tag in {'script','style'} for tag,_ in self.stack):return
        self.text.append(value)
        if self.heading:self.heading[1].append(value)
        if self.anchor:self.anchor['text'].append(value)
        for _,attrs in self.stack:
            marks=(attrs.get('id','')+' '+attrs.get('class','')).lower()
            if re.search(r'\babstract\b',marks):self.sections.setdefault('abstract',[]).append(value);break
        else:
            if self.abstract_heading:self.sections.setdefault('abstract',[]).append(value)
        if any('authors' in a.get('class','').split() or 'author' in a.get('class','').split() for _,a in self.stack):
            if any('name' in a.get('class','').split() for _,a in self.stack):self.authors.append(value)

    def handle_endtag(self, tag):
        if self.heading and tag == self.heading[0]:
            text=' '.join(' '.join(self.heading[1]).split());self.headings.append(text)
            if tag in {'h2','h3'}:self.track=classify_track(text,self.venue)
            self.abstract_heading=text.casefold()=='abstract'
            self.heading=None
        if tag == 'a' and self.anchor:
            self.links.append((self.anchor['href'],' '.join(' '.join(self.anchor['text']).split()),self.anchor['track']))
            self.anchor=None
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0]==tag:del self.stack[index:];break

    def first(self,*keys):
        return next((v for key in keys for v in self.meta.get(key,[]) if v.strip()),'')


def bib_fields(text, year, number):
    """只取与官方页编号完全一致的本文 BibTeX，不能读取参考文献的身份字段。"""
    match=re.search(r'@inproceedings\s*\{\s*ijcai'+str(year)+'p0*'+str(int(number))+r'\s*,',text,re.I)
    if not match:return {}
    depth=1;end=match.end()
    while end<len(text) and depth:
        if text[end]=='{' and (end==0 or text[end-1]!='\\'):depth+=1
        if text[end]=='}' and (end==0 or text[end-1]!='\\'):depth-=1
        end+=1
    if depth:return {}
    value=text[match.end():end-1]
    return {field:re.sub(r'[{}]','',m[1]).strip() for field in ('title','author','year','doi','note','booktitle')
            if (m:=re.search(r'\b'+field+r'\s*=\s*\{((?:[^{}]|\{[^{}]*\})*)\}',value,re.I|re.S))}


def person(value):
    if value.count(',')==1:
        last,first=value.split(',');value=first+' '+last
    return ''.join(c for c in unicodedata.normalize('NFKD',value).casefold() if c.isalnum())


def verify_proceedings(paper,venue,fetch):
    result={'track':'unknown','verification_status':'indexed_unverified','verification_url':None,'verified_at':None,
            'verification_provider':VERIFICATION_PROVIDERS[venue], 'reason':'仅有索引记录，尚未取得可核对的官方论文页。'}
    year=paper.get('year')
    if not isinstance(year,int) or not 1990<=year<=date.today().year:return result
    doi=normalize_doi(paper.get('doi'))
    options=[official_location(u) for u in [paper.get('url'),*paper.get('official_urls',[])]]
    urls={v['url'] for v in options if v and v['venue_slug']==venue and (v['year'] is None or v['year']==year)}
    if not urls and doi:
        if venue=='aaai' and (match:=re.fullmatch(r'10\.1609/aaai\.v\d+i\d+\.(\d+)',doi)):
            urls.add('https://ojs.aaai.org/index.php/AAAI/article/view/'+match[1])
        if venue=='ijcai' and (match:=re.fullmatch(r'10\.24963/ijcai\.(\d{4})/(\d+)',doi)) and int(match[1])==year:
            urls.add(f'https://ijcai.org/proceedings/{year}/{int(match[2])}')
    if len(urls)!=1:return result
    url=next(iter(urls));location=official_location(url)
    try:
        page=ProceedingsPage(venue);page.feed(fetch(url,ttl=7*86400))
        bib=bib_fields(' '.join(page.text),year,location['article_id']) if venue=='ijcai' else {}
        title=page.first('citation_title','dc.title') or bib.get('title') or next(iter(page.headings),'')
        authors=page.meta.get('citation_author') or page.meta.get('dc.creator') or (bib.get('author','').split(' and ') if bib.get('author') else page.authors)
        published=page.first('citation_publication_date','citation_date','dc.date.issued','dc.date') or bib.get('year','')
        official_doi=normalize_doi(page.first('citation_doi','dc.identifier.doi') or bib.get('doi'))
        official_year=re.search(r'\b(19\d{2}|20\d{2})\b',published)
        expected={person(a) for a in paper.get('authors',[])}-{''};actual={person(a) for a in authors}-{''}
        if (not normalize_title(title) or normalize_title(title)!=normalize_title(paper.get('title')) or not expected or not expected<=actual
                or not official_year or int(official_year[1])!=year or (doi and official_doi and doi!=official_doi)):
            result['reason']='官方论文页的标题、作者、年份或 DOI 与候选无法一致核对，保持待核验。';return result
        track='unknown'
        if venue=='aaai':
            issues=[label for href,label,_ in page.links if re.fullmatch(r'/index\.php/AAAI/issue/view/\d+/?',urlsplit(urljoin(url,href)).path)
                    and urlsplit(urljoin(url,href)).hostname=='ojs.aaai.org']
            if len(set(issues))==1:track=classify_track(issues[0],venue)
        else:
            track=classify_track(bib.get('note','') or page.first('citation_section'),venue)
            if track=='unknown':
                try:
                    index_url=f'https://ijcai.org/proceedings/{year}/'
                    index=ProceedingsPage(venue);index.feed(fetch(index_url,ttl=86400))
                    matches={section for href,_,section in index.links if (v:=official_location(urljoin(index_url,href))) and v['url']==url}
                    if len(matches)==1:track=matches.pop()
                except (requests.RequestException,FulltextError):pass
        pdfs=[]
        for raw in [*page.meta.get('citation_pdf_url',[]),*(href for href,label,_ in page.links if re.search(r'\bpdf\b|download',label,re.I))]:
            pdf=urljoin(url,raw);v=official_location(pdf)
            if v and v['venue_slug']==venue and v['article_id']==location['article_id'] and v['year']==location['year']:
                p=urlsplit(pdf)
                if venue=='aaai' and re.fullmatch(r'/index\.php/AAAI/article/(?:download|view)/\d+/\d+(?:/\d+)?',p.path):
                    pdfs.append('https://ojs.aaai.org'+p.path.replace('/article/view/','/article/download/'))
                elif venue=='ijcai' and p.path.endswith('.pdf'):
                    pdfs.append('https://ijcai.org'+p.path)
        result.update(track=track,verification_status='official_verified',verification_url=url,
                      verified_at=datetime.now(timezone.utc).isoformat(),reason='已核对官方论文页的标题、作者、年份及可取得的 DOI；类别按官方记录标注。',
                      abstract=' '.join((' '.join(page.sections.get('abstract',[])) or page.first('citation_abstract','dc.description')).split()),pdf_url=next(iter(pdfs),None))
        return result
    except (requests.RequestException,FulltextError):
        result['reason']='官方论文集访问受限或暂不可用，未完成发表核验；不能据此判定论文未录用。'
        return result


def find_proceedings_pdf_urls(title,authors,doi,source_url,fetch):
    location=official_location(source_url)
    if not location:raise FulltextError('NO_PROCEEDINGS_PDF','没有明确的 AAAI/IJCAI 官方论文页。')
    year=location['year']
    if year is None:
        page=ProceedingsPage('aaai');page.feed(fetch(location['url']))
        match=re.search(r'\b(19\d{2}|20\d{2})\b',page.first('citation_publication_date','citation_date','dc.date.issued'))
        year=int(match[1]) if match else None
    result=verify_proceedings({'title':title,'authors':authors,'doi':doi,'url':source_url,'year':year},location['venue_slug'],fetch)
    if result['verification_status']!='official_verified' or not result.get('pdf_url'):
        raise FulltextError('NO_PROCEEDINGS_PDF','官方身份未确认或未取得正文 PDF 地址，请尝试其他来源或手动上传。')
    return [result['pdf_url']]
