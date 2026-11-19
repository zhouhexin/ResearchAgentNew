"""阅读输入身份；包含真实片段编号、内容和位置，不仅比较全文哈希。"""
from agent.runtime_snapshot import fingerprint


def context_identity(context):
    return fingerprint({'paper_id': context.paper_id, 'title': context.paper_title, 'doi': context.doi,
        'document_id': context.document_id, 'hash': context.document_sha256, 'source': context.source_url,
        'total_chunks': context.total_chunks, 'reading_selection': context.reading_selection,
        'sources': [{'id': key, 'text': value.text, 'page': value.page_number, 'section': value.section_hint}
                    for key, value in context.sources.items()]})
