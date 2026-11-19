"""查询定位只排列真实片段；不让模型生成证据编号或宣称语义匹配。"""
import re
from agent.runtime_snapshot import fingerprint

POLICY = 'targeted-reading-v1'


def terms(text):
    words = re.findall(r'[a-z0-9]{2,}', text.lower())
    for group in re.findall(r'[\u4e00-\u9fff]+', text):
        words.extend(group[i:i+2] for i in range(len(group)-1))
    return set(words)


def candidates(context, question, target, read_ids, locator='', limit=40):
    needles = terms(' '.join((question, target, locator)))
    ranked = []
    for index, (key, source) in enumerate(context.sources.items()):
        haystack = (source.section_hint or '') + '\n' + source.text
        score = len(needles & terms(haystack))
        ranked.append((score, key not in read_ids, -index, key, source))
    ranked.sort(key=lambda row: row[:3], reverse=True)
    return [{'evidence_id': key, 'page_number': source.page_number, 'section_hint': source.section_hint,
             'already_read': key in read_ids, 'lexical_matches': score,
             'characters': len(source.text), 'text': source.text,
             'text_sha256': fingerprint(source.text)} for score, _, _, key, source in ranked[:limit]]
