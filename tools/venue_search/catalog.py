"""支持的会议/期刊目录；发表场所与检索平台分开，不推测未导入的目录等级。"""
CVPR = {'slug': 'cvpr', 'name': 'IEEE/CVF Conference on Computer Vision and Pattern Recognition',
        'acronym': 'CVPR', 'venue_type': 'conference', 'field': 'computer_vision',
        'official_url': 'https://cvpr.thecvf.com/', 'ranking': None,
        'ranking_notice': '目录等级尚未导入；发表身份与目录等级分开核验。'}
ICCV = {'slug': 'iccv', 'name': 'IEEE/CVF International Conference on Computer Vision',
        'acronym': 'ICCV', 'venue_type': 'conference', 'field': 'computer_vision',
        'official_url': 'https://iccv.thecvf.com/', 'ranking': None,
        'ranking_notice': '目录等级尚未导入；发表身份与目录等级分开核验。'}
AAAI = {'slug': 'aaai', 'name': 'AAAI Conference on Artificial Intelligence', 'acronym': 'AAAI',
        'venue_type': 'conference', 'field': 'artificial_intelligence', 'official_url': 'https://aaai.org/conference/aaai/',
        'ranking': None, 'ranking_notice': '目录等级尚未导入；发表身份与目录等级分开核验。'}
IJCAI = {'slug': 'ijcai', 'name': 'International Joint Conference on Artificial Intelligence', 'acronym': 'IJCAI',
         'venue_type': 'conference', 'field': 'artificial_intelligence', 'official_url': 'https://ijcai.org/',
         'ranking': None, 'ranking_notice': '目录等级尚未导入；发表身份与目录等级分开核验。'}
def _venue(slug, name, acronym, field, official_url, venue_type='conference'):
    return dict(slug=slug, name=name, acronym=acronym, field=field, official_url=official_url,
                venue_type=venue_type, ranking=None, ranking_notice=CVPR['ranking_notice'])


VENUES = {'cvpr': CVPR, 'iccv': ICCV, 'aaai': AAAI, 'ijcai': IJCAI,
    'acl': _venue('acl', 'Annual Meeting of the Association for Computational Linguistics', 'ACL', 'natural_language_processing', 'https://aclanthology.org/venues/acl/'),
    'emnlp': _venue('emnlp', 'Conference on Empirical Methods in Natural Language Processing', 'EMNLP', 'natural_language_processing', 'https://aclanthology.org/venues/emnlp/'),
    'sigir': _venue('sigir', 'International ACM SIGIR Conference on Research and Development in Information Retrieval', 'SIGIR', 'information_retrieval', 'https://sigir.org/'),
    'neurips': _venue('neurips', 'Conference on Neural Information Processing Systems', 'NeurIPS', 'machine_learning', 'https://neurips.cc/'),
    'artificial-intelligence': _venue('artificial-intelligence', 'Artificial Intelligence', 'AIJ', 'artificial_intelligence', 'https://www.sciencedirect.com/journal/artificial-intelligence', 'journal'),
    'tois': _venue('tois', 'ACM Transactions on Information Systems', 'TOIS', 'information_retrieval', 'https://dl.acm.org/journal/tois', 'journal'),
    'eccv': _venue('eccv', 'European Conference on Computer Vision', 'ECCV', 'computer_vision', 'https://www.ecva.net/'),
    'icml': _venue('icml', 'International Conference on Machine Learning', 'ICML', 'machine_learning', 'https://icml.cc/'),
    'iclr': _venue('iclr', 'International Conference on Learning Representations', 'ICLR', 'machine_learning', 'https://iclr.cc/')}

# 唯一枚举供 API 和模型结构化输出共用，增项时只维护此处。
from typing import Literal
VenueSlug = Literal['cvpr','iccv','aaai','ijcai','acl','emnlp','sigir','neurips',
                    'artificial-intelligence','tois','eccv','icml','iclr']
JOURNAL_ISSNS = {'artificial-intelligence': {'0004-3702'}, 'tois': {'1046-8188','1558-2868'}}
VENUE_PLATFORMS = {
    'cvpr': ['cvf'], 'iccv': ['cvf'], 'aaai': ['aaai_proceedings'], 'ijcai': ['ijcai_proceedings'],
    'acl': ['acl_anthology'], 'emnlp': ['acl_anthology'], 'sigir': ['acm','crossref'],
    'neurips': ['neurips_proceedings','openreview'], 'artificial-intelligence': ['elsevier','crossref'],
    'tois': ['acm','crossref'], 'eccv': ['ecva','springer','crossref','openreview'],
    'icml': ['pmlr','openreview'], 'iclr': ['openreview','iclr_proceedings']}

SOURCE_QUERIES = {'cvpr': 'computer vision and pattern recognition', 'iccv': 'international conference on computer vision',
                  'aaai': 'aaai conference on artificial intelligence', 'ijcai': 'international joint conference on artificial intelligence',
                  'acl': 'annual meeting of the association for computational linguistics',
                  'emnlp': 'empirical methods in natural language processing',
                  'sigir': 'research and development in information retrieval',
                  'neurips': 'neural information processing systems', 'artificial-intelligence': 'Artificial Intelligence',
                  'tois': 'ACM Transactions on Information Systems', 'eccv': 'european conference on computer vision',
                  'icml': 'international conference on machine learning', 'iclr': 'international conference on learning representations'}
VERIFICATION_PROVIDERS = {'cvpr': 'CVF', 'iccv': 'CVF', 'aaai': 'AAAI Proceedings', 'ijcai': 'IJCAI Proceedings',
    'acl':'ACL Anthology','emnlp':'ACL Anthology','sigir':'ACM / Crossref','neurips':'NeurIPS Proceedings',
    'artificial-intelligence':'Elsevier / Crossref','tois':'ACM / Crossref','eccv':'ECVA / Springer',
    'icml':'PMLR','iclr':'OpenReview / ICLR Proceedings'}


def supported_sources(slugs):
    return ({'openalex','openalex_venues',*('openalex_'+s for s in slugs),
             *(p for s in slugs for p in VENUE_PLATFORMS[s])} if slugs else {'arxiv'})


def verification_provider(slug, url=None):
    from urllib.parse import urlsplit
    host=urlsplit(url or '').hostname
    return {'api.crossref.org':'Crossref 出版记录','openreview.net':'OpenReview',
            'proceedings.iclr.cc':'ICLR Proceedings'}.get(host,VERIFICATION_PROVIDERS.get(slug,'官方发表记录'))



def selected_venues(scope):
    """兼容已保存的单会议计划/检查点。"""
    return sorted(scope.get('venue_slugs') or ([scope['venue_slug']] if scope.get('venue_slug') else []))


def validate_venues(slugs):
    if not slugs or len(slugs) > len(VENUES) or len(set(slugs)) != len(slugs) or any(s not in VENUES for s in slugs):
        raise ValueError('请选择至少一个已支持的会议或期刊：'+ '、'.join(v['acronym'] for v in VENUES.values())+'；不能重复或指定尚未接通的会议或期刊。')
    return sorted(slugs)
