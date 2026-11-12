"""按正文标题选择原始证据窗口，不重新切片、不改写引用。"""
from bisect import bisect_right
from collections import defaultdict
import re

VERSION='body-section-priority-v1'
DEFAULT_BUDGET=18
LABELS={'overview':'摘要与研究问题','method':'方法与技术方案','experiments':'实验与结果',
        'conclusion':'结论与局限','background':'背景与相关工作','body':'其他正文',
        'references':'参考文献','appendix':'附录与补充材料'}
PRIORITY=('overview','method','experiments','conclusion')


def heading_kind(line):
    title=line.strip().strip('#').strip()
    if not title or len(title)>110:return None
    title=re.sub(r'^(?:\d+(?:\.\d+)*[.)]?|[IVX]+[.)]|第[一二三四五六七八九十\d]+[章节])\s*','',title)
    title=title.strip(' .:：').casefold()
    if re.fullmatch(r'references|bibliography|参考文献',title):return 'references'
    if re.match(r'^(?:appendix|appendices|supplementary material|supplemental material|附录)(?:$|\s+[a-z](?:$|[ .:])|\s*[:：])',title):return 'appendix'
    if re.fullmatch(r'acknowledg(?:e)?ments?|致谢',title):return 'background'
    patterns={
        'overview':r'abstract|introduction|motivation|problem (?:definition|formulation|statement)|task definition|摘要|引言|绪论|问题定义|研究问题',
        'method':r'(?:our |proposed )?(?:method(?:s|ology)?|approach|framework|architecture|model|system)(?:\s*[:：]\s*[^.!?]+)?|方法|研究方法|模型设计|技术方案',
        'experiments':r'experimental setup|evaluation setup|main results|performance evaluation|(?:experimental |empirical )?(?:experiments?|evaluation|results?|analysis|ablation(?: study| studies)?|implementation details)(?: and (?:results|discussion|analysis))?(?:\s*[:：]\s*[^.!?]+)?|实验|实验设置|实验结果|实验与结果|结果分析',
        'conclusion':r'conclusions?(?: and (?:future work|limitations|discussion))?|limitations?(?: and (?:future work|conclusion))?|discussion(?: and (?:conclusion|limitations))?|future work|结论|总结|局限性|研究局限|结论与展望',
        'background':r'related work|background|preliminaries|相关工作|背景|预备知识'}
    return next((kind for kind,pattern in patterns.items() if re.fullmatch(pattern,title)),None)


def section_ranges(text):
    events=[(0,'body','未识别章节')]
    for match in re.finditer(r'(?m)^[^\n\r]+',text):
        kind=heading_kind(match[0])
        if kind:events.append((match.start(),kind,match[0].strip()))
    return sorted(events,key=lambda x:x[0])


def choose_chunks(chunks,text,budget=DEFAULT_BUDGET):
    if not 8<=budget<=50:raise ValueError('快速阅读片段上限须为8–50，确保有空间覆盖关键章节。')
    sections=section_ranges(text);starts=[s[0] for s in sections]
    pools=defaultdict(list);kinds={};titles={}
    for index,chunk in enumerate(chunks):
        # 按窗口与各章节的实际重合长度归类；章节边界窗口不会因陈旧 section_hint 误判。
        overlaps=defaultdict(int);overlap_titles={}
        pos=max(0,bisect_right(starts,chunk.start_char)-1)
        while pos<len(sections) and starts[pos]<chunk.end_char:
            begin,kind,title=sections[pos];end=starts[pos+1] if pos+1<len(sections) else len(text)
            overlaps[kind]+=max(0,min(end,chunk.end_char)-max(begin,chunk.start_char));overlap_titles[kind]=title
            pos+=1
        # 只要覆盖正文关键章节，就保留在该章节候选池中，避免与参考文献共用窗口时漏掉结论。
        important=[k for k in PRIORITY if overlaps.get(k,0)>0]
        kind=max(important or list(overlaps) or ['body'],key=lambda k:overlaps.get(k,0))
        kinds[index]=kind;titles[index]=overlap_titles.get(kind,'未识别章节')
        for category in important or [kind]:pools[category].append(index)
    selected=[];seen=set()
    def add(index):
        if index not in seen and len(selected)<budget:selected.append(index);seen.add(index)
    # 每个关键章节先选开头和末尾，随后轮流补充内部窗口，避免摘要占满预算。
    for kind in PRIORITY:
        values=pools[kind]
        if values:add(values[0])
    for kind in PRIORITY:
        values=pools[kind]
        if len(values)>1:add(values[-1])
    if not selected and pools['body']:
        add(pools['body'][0]);add(pools['body'][-1])
    def distributed(values):
        if not values:return []
        order=[];queue=[(0,len(values)-1)]
        while queue:
            lo,hi=queue.pop(0)
            if lo>hi:continue
            mid=(lo+hi)//2;order.append(values[mid]);queue.extend(((lo,mid-1),(mid+1,hi)))
        return order
    orders={k:distributed(pools[k]) for k in PRIORITY}
    for offset in range(max((len(v) for v in orders.values()),default=0)):
        for kind in PRIORITY:
            if offset<len(orders[kind]):add(orders[kind][offset])
    for kind in ('body','background'):
        for index in distributed(pools[kind]):add(index)
    # 只有参考文献/附录可用时明确拒绝，不伪装成正文阅读。
    if not selected:raise ValueError('未识别到可用于快速阅读的正文片段，请使用全量分析或检查全文。')
    selected.sort()  # 正文顺序输入模型，但预算分配优先保证关键章节。
    missing=[LABELS[k] for k in PRIORITY if not pools[k]]
    metadata={'scope':'fast','policy_version':VERSION,'budget':budget,
              'strategy':'section_priority' if any(pools[k] for k in PRIORITY) else 'body_fallback',
              'total_chunks':len(chunks),'selected_chunks':len(selected),
              'selected_pages':sorted({chunks[i].page_number for i in selected if chunks[i].page_number is not None}),
              'sections':[{'category':k,'label':LABELS[k],'available_chunks':len(pools[k]),'selected_chunks':sum(i in pools[k] for i in selected)} for k in LABELS],
              'missing_sections':missing,
              'note':'快速阅读仅覆盖所选片段；优先正文关键章节，参考文献和附录不主动纳入，不能声称已通读全文。跨章节片段可能计入多个章节。'+(' 部分关键章节标题未识别，已对其他正文补充取样，请核对阅读范围。' if missing else '')}
    return [chunks[i] for i in selected],metadata
