"""按原始字符位置生成段落/句子窗口；不改写或重新拼接原文。"""
from bisect import bisect_left, bisect_right
import re

PARAGRAPH_END = re.compile(r'\n[ \t]*\n(?:[ \t]*\n)*[ \t]*')
SENTENCE_END = re.compile(r'(?P<punct>[。！？!?]+|\.+)[”’"\u0027）)\]]*')
WORD_BREAK = re.compile(r'\s+|[，；,;]')
ABBREVIATIONS = {'e.g', 'i.e', 'etc', 'al', 'fig', 'eq', 'sec', 'dr', 'mr', 'mrs', 'ms',
                 'prof', 'vs', 'no', 'vol', 'pp', 'p', 'st', 'jr', 'sr', 'approx', 'cf'}


def sentence_boundaries(text):
    boundaries = {0, len(text)}
    for match in SENTENCE_END.finditer(text):
        punctuation = match.group('punct')
        end = match.end()
        following_index = end
        while following_index < len(text) and text[following_index].isspace():
            following_index += 1
        if punctuation.startswith('.'):
            # 不把省略号、单词内部的点、常用缩写、姓名首字母和数字小数拆成句尾。
            if punctuation != '.' or (end < len(text) and not text[end].isspace()):
                continue
            prefix = text[max(0, match.start() - 32):match.start()]
            token = re.search(r'([A-Za-z]+(?:\.[A-Za-z]+)*)$', prefix)
            word = token.group(1) if token else ''
            if word.lower() in ABBREVIATIONS or re.fullmatch(r'(?:[A-Za-z]\.)+[A-Za-z]', word):
                continue
            following = text[following_index:following_index + 1]
            if len(word) == 1 and word.isupper() and following.isupper():
                continue
            if prefix[-1:].isdigit() and following.isdigit():
                continue
        boundaries.add(following_index)
    return sorted(boundaries)


def resolve_chunk_overlap(chunk_size, chunk_overlap):
    if not isinstance(chunk_size, int) or chunk_size < 100:
        raise ValueError('chunk_size 至少为 100，且须为整数')
    if chunk_overlap is None:
        chunk_overlap = min(200, chunk_size // 5)
    if not isinstance(chunk_overlap, int) or not 0 <= chunk_overlap < chunk_size:
        raise ValueError('chunk_overlap 须为非负整数，且小于 chunk_size')
    return chunk_overlap


def text_windows(text, *, chunk_size=1200, chunk_overlap=None):
    """返回 (start, end)。重叠量是上限；完整句子放不进重叠预算时不强行切开它。"""
    chunk_overlap = resolve_chunk_overlap(chunk_size, chunk_overlap)
    paragraphs = sorted({match.end() for match in PARAGRAPH_END.finditer(text)} | {len(text)})
    sentences = sentence_boundaries(text)
    natural = sorted(set(paragraphs) | set(sentences))
    words = [match.end() for match in WORD_BREAK.finditer(text)]

    def last_boundary(boundaries, start, limit):
        index = bisect_right(boundaries, limit) - 1
        return boundaries[index] if index >= 0 and boundaries[index] > start else None

    def window_end(start):
        limit = min(start + chunk_size, len(text))
        if limit == len(text):
            return limit
        paragraph = last_boundary(paragraphs, start, limit)
        if paragraph is not None and paragraph - start >= chunk_size // 2:
            return paragraph
        sentence = last_boundary(sentences, start, limit)
        if sentence is not None:
            return sentence
        # 单个句子超出窗口上限时，尽量不从单词中间截断。
        word = last_boundary(words, start, limit)
        return word if word is not None and word - start >= chunk_size // 2 else limit

    start, previous_end = 0, 0
    natural_set = set(natural)
    while start < len(text):
        end = window_end(start)
        if end <= previous_end:
            # 若保留上下文导致只容得下上一窗口的句子，则让新句子优先完整进入。
            start = previous_end
            end = window_end(start)
        yield start, end
        if end == len(text):
            break
        lower = max(start + 1, end - chunk_overlap)
        index = bisect_left(natural, lower)
        if chunk_overlap and index < len(natural) and natural[index] < end:
            next_start = natural[index]
        elif chunk_overlap and end not in natural_set:
            index = bisect_left(words, lower)
            next_start = words[index] if index < len(words) and words[index] < end else lower
        else:
            next_start = end
        previous_end, start = end, next_start
