"""从固定分析快照构建阅读用引用；不改写编号、引句或模型结论。"""
from agent.analysis_schema import PaperAnalysis
from review.loader import CATEGORIES, ReviewPaper


def build_citation_index(draft, papers):
    # 与正文呈现顺序一致；重复引用复用同一个脚注。
    from review.synthesis import SECTIONS, point_references

    refs = dict.fromkeys(
        ref for section in SECTIONS for point in getattr(draft, section)
        for ref in point_references(point)
    )
    sources, quotes = {}, {}
    for number, paper in enumerate(papers, 1):
        for evidence_id, source in paper.evidence_index.items():
            ref = f'P{paper.paper_id}:{evidence_id}'
            sources[ref] = (number, paper, source)
        for category in CATEGORIES:
            for finding in getattr(paper.analysis, category):
                for evidence in finding.evidence:
                    ref = f'P{paper.paper_id}:{evidence.evidence_id}'
                    source = paper.evidence_index.get(evidence.evidence_id, {})
                    # 只能展示该快照中原文核验成立的引句；不按相似度猜测。
                    if evidence.quote.strip() and evidence.quote in source.get('text', ''):
                        values = quotes.setdefault(ref, [])
                        if evidence.quote not in values:
                            values.append(evidence.quote)
    result = {}
    for footnote, ref in enumerate(refs, 1):
        number, paper, source = sources[ref]
        exact_quotes = quotes.get(ref, [])
        result[ref] = {
            'ref_id': ref, 'paper_id': paper.paper_id, 'paper_number': number,
            'paper_title': paper.title, 'page_number': source.get('page_number'),
            'source_url': source['source_url'], 'footnote_id': f'evidence-{footnote}',
            'quotes': exact_quotes or [source['text']],
            'quote_kind': 'analysis_quotes' if exact_quotes else 'source_chunk',
        }
    return result


def snapshot_papers(payload):
    """仅用报告自身的冻结材料恢复展示，不读取较新的单篇分析。"""
    rows = {row['paper_id']: row for row in payload['matrix']}
    papers = []
    for paper_id in payload['paper_ids']:
        row = rows[paper_id]
        prefix = f'P{paper_id}:'
        index = {ref[len(prefix):]: source for ref, source in payload['evidence_index'].items()
                 if ref.startswith(prefix)}
        papers.append(ReviewPaper(
            paper_id=paper_id, title=row['title'], doi=row.get('doi'),
            document_id=row.get('document_id', 0), document_hash=row.get('content_sha256', ''),
            source_url=next(iter(index.values()), {}).get('source_url', ''),
            analysis=PaperAnalysis.model_validate(row['analysis']), evidence_index=index,
        ))
    return papers
