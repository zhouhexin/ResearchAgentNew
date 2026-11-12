"""页面和 CLI 共用的模型检查样例；不读取或写入论文数据库。"""
from agent.analysis_validator import PaperContext, EvidenceSource, CATEGORIES
from agent.paper_analyzer import analyze_paper


def check_structured_analysis(llm, method):
    context = PaperContext(
        paper_id=0, paper_title='模型结构化分析诊断样例', doi=None,
        document_id=0, document_sha256='diagnostic', source_url='diagnostic', total_chunks=2,
        sources={
            'E1': EvidenceSource('E1',
                'We study whether retrieving source passages improves factual accuracy. '
                'Our method retrieves two passages for each question and generates an answer using only those passages. '
                'Every generated claim must cite an exact quote from a retrieved passage.', 'diagnostic', 1, 'Method'),
            'E2': EvidenceSource('E2',
                'We evaluate the method on a synthetic dataset of 100 questions. '
                'The baseline answers 60 questions correctly, while the retrieval method answers 80 correctly. '
                'The study is limited to synthetic English questions and does not evaluate other languages.',
                'diagnostic', 2, 'Results'),
        })
    analysis = analyze_paper(llm, context, method=method, max_repair_attempts=0)
    if not any(getattr(analysis, category) for category in CATEGORIES):
        raise ValueError('结构化检查返回了全空分析，未验证到有效的原文引用')
    return {
        'findings': sum(len(getattr(analysis, category)) for category in CATEGORIES),
        'quotes': sum(len(finding.evidence) for category in CATEGORIES for finding in getattr(analysis, category)),
    }
