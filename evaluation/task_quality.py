"""P3 离线任务级核验；不执行模型，不自动导入业务材料，不把机器标签当人工答案。"""
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer
from agent.runtime_snapshot import fingerprint

POLICY='task-quality-v1'


class InvalidEvaluation(ValueError):
    code='TASK_EVALUATION_INVALID'


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class Permission(Strict):
    allowed:bool
    basis:str=Field(min_length=1,max_length=2000)
    granted_by:str=Field(min_length=1,max_length=200)
    granted_at:str=Field(min_length=1,max_length=100)


class Material(Strict):
    id:str=Field(min_length=1,max_length=100)
    paper_key:str=Field(min_length=1,max_length=100)
    document_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    text:str=Field(min_length=1)
    text_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    page_number:int|None=Field(default=None,ge=1)

    @model_validator(mode='after')
    def valid(self):
        if fingerprint(self.text)!=self.text_sha256:raise ValueError('材料文本与冻结哈希不一致。')
        return self


class Requirement(Strict):
    id:str=Field(min_length=1,max_length=100)
    description:str=Field(min_length=1,max_length=2000)
    author:str=Field(min_length=1)
    rationale:str=Field(min_length=1)


class Case(Strict):
    id:str=Field(min_length=1,max_length=100)
    revision:str=Field(min_length=1)
    group_id:str=Field(min_length=1,max_length=100)
    split:Literal['development','regression','holdout']
    kind:Literal['natural','synthetic']
    topic:str=Field(min_length=1)
    task_type:str=Field(min_length=1)
    question:str=Field(min_length=1,max_length=6000)
    scope:str=Field(min_length=1,max_length=6000)
    permission:Permission
    materials:list[Material]=Field(min_length=1,max_length=100)
    requirements:list[Requirement]=Field(min_length=1,max_length=30)
    review_request:dict|None=None
    source_feedback:dict|None=None

    @model_serializer(mode='wrap')
    def compatible_dump(self, handler):
        value = handler(self)
        for key in ('review_request', 'source_feedback'):
            if value.get(key) is None:value.pop(key, None)
        return value

    @model_validator(mode='after')
    def valid(self):
        for values in (self.materials,self.requirements):
            if len({v.id for v in values})!=len(values):raise ValueError('案例材料或要求编号重复。')
        if self.review_request is not None:
            from evaluation.report_protocol import check_case
            check_case(self)
        if self.source_feedback is not None:
            from evaluation.report_protocol import check_feedback
            check_feedback(self.source_feedback)
        return self

    @property
    def input_hash(self):
        value = {k:getattr(self,k) for k in ('question','scope')}|{
            'materials':[m.model_dump() for m in self.materials]}
        if self.review_request is not None:value['review_request']=self.review_request
        return fingerprint(value)


class Dataset(Strict):
    protocol:Literal['task-dataset-v1']='task-dataset-v1'
    id:str=Field(min_length=1)
    revision:str=Field(min_length=1)
    annotation_version:str=Field(min_length=1)
    cases:list[Case]=Field(min_length=1)

    @model_validator(mode='after')
    def valid(self):
        if len({c.id for c in self.cases})!=len(self.cases):raise ValueError('案例编号重复。')
        seen={}
        for case in self.cases:
            # 同组、同论文身份或同文档哈希均不得跨集合；防止同源材料泄漏。
            tokens=[('group',case.group_id)]+[(k,v) for m in case.materials for k,v in
                [('paper',m.paper_key),('document',m.document_sha256),('text',m.text_sha256)]]
            for token in tokens:
                if token in seen and seen[token]!=case.split:raise ValueError('同源材料跨集合泄漏。')
                seen[token]=case.split
        return self

    @property
    def sha256(self):return fingerprint(self.model_dump())


class Citation(Strict):
    material_id:str
    quote:str=Field(min_length=1)
    start_char:int=Field(ge=0)
    end_char:int=Field(ge=1)


class Finding(Strict):
    statement:str=Field(min_length=1)
    citations:list[Citation]=Field(min_length=1)


class Judgment(Strict):
    requirement_id:str
    verdict:Literal['met','partial','unmet','uncertain']
    assessor:str=Field(min_length=1)
    reason:str=Field(min_length=1)
    context:Literal['independent','after_model_inspection']
    previous_verdict:Literal['met','partial','unmet','uncertain']|None=None


class Outcome(Strict):
    case_id:str
    repetition:int=Field(ge=1)
    input_hash:str=Field(pattern='^[a-f0-9]{64}$')
    status:Literal['succeeded','failed','interrupted']
    answer:str
    findings:list[Finding]
    human_judgments:list[Judgment]
    error_code:str|None=None
    seconds:float|None=Field(default=None,ge=0,allow_inf_nan=False)
    calls:int|None=Field(default=None,ge=0)
    tokens:int|None=Field(default=None,ge=0)
    cost:float|None=Field(default=None,ge=0,allow_inf_nan=False)

    @model_validator(mode='after')
    def valid(self):
        if self.status=='succeeded' and not self.answer.strip():raise ValueError('成功任务必须有回答。')
        if self.status!='succeeded' and not self.error_code:raise ValueError('失败/中断任务须记录错误类型。')
        if len({v.requirement_id for v in self.human_judgments})!=len(self.human_judgments):raise ValueError('人工要求判定重复。')
        return self


class Run(Strict):
    protocol:Literal['task-run-v1']='task-run-v1'
    id:str=Field(min_length=1)
    kind:Literal['controlled_fixture','recorded_run']
    dataset_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    split:Literal['development','regression','holdout']
    mode:Literal['frozen','live_search']
    repetitions:int=Field(ge=1,le=20)
    code_fingerprint:str=Field(pattern='^[a-f0-9]{64}$')
    runtime_snapshot_id:str=Field(min_length=1)
    prompt_schema_fingerprint:str=Field(pattern='^[a-f0-9]{64}$')
    tool_config_fingerprint:str=Field(pattern='^[a-f0-9]{64}$')
    outcomes:list[Outcome]


def load(path,model):
    try:return model.model_validate(json.loads(Path(path).read_text(encoding='utf-8')))
    except (ValueError,TypeError):raise InvalidEvaluation(f'{model.__name__} 格式或内容核验失败。') from None


def check_run(dataset,run,allow_holdout=False):
    if run.dataset_sha256!=dataset.sha256:raise InvalidEvaluation('结果不属于此冻结数据集/人工要求版本。')
    if run.split=='holdout' and not allow_holdout:raise InvalidEvaluation('留出结果需显式 --allow-holdout；不会默认读取留出答案。')
    selected={c.id:c for c in dataset.cases if c.split==run.split}
    if not selected:raise InvalidEvaluation('所选集合没有案例。')
    if any(not c.permission.allowed for c in selected.values()):raise InvalidEvaluation('所选材料未获评测授权或已撤回。')
    seen=set()
    for outcome in run.outcomes:
        case=selected.get(outcome.case_id)
        key=(outcome.case_id,outcome.repetition)
        if not case or key in seen or outcome.repetition>run.repetitions:raise InvalidEvaluation('未知、重复案例或重复轮次越界。')
        seen.add(key)
        if outcome.input_hash!=case.input_hash:raise InvalidEvaluation('实际任务输入与冻结问题/材料不匹配。')
        if any(j.requirement_id not in {r.id for r in case.requirements} for j in outcome.human_judgments):
            raise InvalidEvaluation('人工判定包含未知要求。')
    return selected


def score(dataset,run,allow_holdout=False):
    selected=check_run(dataset,run,allow_holdout)
    actual={(o.case_id,o.repetition):o for o in run.outcomes}
    rows=[]
    for case in selected.values():
        for repetition in range(1,run.repetitions+1):
            outcome=actual.get((case.id,repetition));issues=[]
            if outcome:
                sources={m.id:m for m in case.materials}
                for i,f in enumerate(outcome.findings):
                    for j,c in enumerate(f.citations):
                        source=sources.get(c.material_id)
                        if not source or not 0<=c.start_char<c.end_char<=len(source.text) or source.text[c.start_char:c.end_char]!=c.quote:
                            issues.append({'finding_index':i,'citation_index':j,'reason':'source_span_mismatch'})
            status=outcome.status if outcome else 'missing'
            source_status=('failed' if issues else 'passed' if outcome and outcome.findings else 'not_checked')
            by_requirement={j.requirement_id:j.model_dump() for j in outcome.human_judgments} if outcome else {}
            judgments=[{'requirement_id':r.id,'verdict':'not_reviewed','assessor':None,'reason':None,'context':None}|by_requirement.get(r.id,{}) for r in case.requirements]
            rows.append({'case_id':case.id,'repetition':repetition,'group_id':case.group_id,'kind':case.kind,'topic':case.topic,'task_type':case.task_type,
                'status':status,'error_code':outcome.error_code if outcome else 'RESULT_MISSING',
                'source_status':source_status,'source_issues':issues,'human_judgments':judgments,
                'usage':{k:getattr(outcome,k) if outcome else None for k in ('seconds','calls','tokens','cost')}})
    def metrics(values):
        statuses=Counter(r['status'] for r in values);sources=Counter(r['source_status'] for r in values)
        independent=Counter(j['verdict'] for r in values if r['status']=='succeeded' for j in r['human_judgments'] if j['context']=='independent')
        influenced=Counter(j['verdict'] for r in values if r['status']=='succeeded' for j in r['human_judgments'] if j['context']=='after_model_inspection')
        non_success=Counter(j['verdict'] for r in values if r['status']!='succeeded' for j in r['human_judgments'] if j['context'] is not None)
        total_requirements=sum(len(r['human_judgments']) for r in values)
        usage={}
        for key in ('seconds','calls','tokens','cost'):
            known=[r['usage'][key] for r in values if r['usage'][key] is not None]
            usage[key]={'known_count':len(known),'total_count':len(values),'known_sum':sum(known) if known else None}
        return {'task_denominator':len(values),'statuses':dict(statuses),
            'execution_success_fraction':{'numerator':statuses['succeeded'],'denominator':len(values)},
            'source_checks':dict(sources),'source_pass_fraction':{'numerator':sources['passed'],'denominator':len(values)},
            'requirement_denominator':total_requirements,'independent_human':dict(independent),
            'after_model_inspection':dict(influenced),'non_success_human':dict(non_success),
            'unreviewed_requirements':sum(j['context'] is None for r in values for j in r['human_judgments']),
            'usage':usage}
    groups=defaultdict(list)
    for row in rows:groups[row['kind']].append(row)
    return {'policy':POLICY,'dataset_sha256':dataset.sha256,'run_id':run.id,'run_kind':run.kind,'mode':run.mode,'split':run.split,
        'version_record':{k:getattr(run,k) for k in ('code_fingerprint','runtime_snapshot_id','prompt_schema_fingerprint','tool_config_fingerprint')},
        'metrics':metrics(rows),'by_sample_kind':{k:metrics(v) for k,v in groups.items()},'rows':rows,
        'limits':['结果文件和人工身份为提供方声明，尚未与业务执行注册表核对；不是实际回放证明。',
                  '来源字面核验不等于语义支持；回答程度仅统计人工声明，模型标签不得充当人工标准。',
                  '不因缺失/失败样本缩小分母，不把未知成本视为零；合成案例不代表真实错误率。',
                  '本工具不调用模型、检索或修改生产版本，不输出自动晋级决定。']}


def compare(dataset,left,right,allow_holdout=False):
    if left.mode!=right.mode or left.split!=right.split or left.repetitions!=right.repetitions:
        raise InvalidEvaluation('只能成对比较同集合、同轮次、同检索模式的结果。')
    if left.kind!=right.kind:raise InvalidEvaluation('受控替身和实际记录不能混作同类运行比较。')
    a=score(dataset,left,allow_holdout);b=score(dataset,right,allow_holdout)
    paired=[]
    for x,y in zip(a['rows'],b['rows']):
        paired.append({'case_id':x['case_id'],'repetition':x['repetition'],
            'changed_fields':[k for k in ('status','error_code','source_status','source_issues','human_judgments','usage') if x[k]!=y[k]],
            'left':x,'right':y})
    return {'policy':POLICY,'dataset_sha256':dataset.sha256,'left':a,'right':b,'paired_rows':paired,
            'changed_pairs':sum(bool(p['changed_fields']) for p in paired),'promotion_decision':'not_evaluated'}


def markdown(report):
    runs=[report['left'],report['right']] if 'paired_rows' in report else [report]
    lines=['# 任务级离线评测记录','','> 来源校验、运行结果、人工声明和用量分别统计；不自动晋级。','']
    for run in runs:
        lines.extend([f"## {run['run_id']} · {run['run_kind']}",'',f"模式：{run['mode']}；集合：{run['split']}；数据集：{run['dataset_sha256']}",
            '', '```json',json.dumps(run['metrics'],ensure_ascii=False,indent=2),'```','',
            '|案例|轮次|样本|运行状态|来源校验|错误类型|','|---|---|---|---|---|---|'])
        for r in run['rows']:lines.append('|'+ '|'.join(str(r[k]).replace('|','\\|').replace('\n',' ') for k in ('case_id','repetition','kind','status','source_status','error_code'))+'|')
        lines.extend(['','### 逐例判定与错误',''])
        for r in run['rows']:
            lines.append(f"- {r['case_id']} / {r['repetition']}：{r['status']}；{r['source_status']}；错误：{r['error_code']}")
            for issue in r['source_issues']:lines.append('  - 来源错误：'+json.dumps(issue,ensure_ascii=False))
            for j in r['human_judgments']:lines.append(f"  - 要求 {j['requirement_id']}：{j['verdict']}；背景 {j['context']}；复核人 {j['assessor']}；理由 {j['reason']}".replace('\n',' '))
        lines.extend(['',*('- '+v for v in run['limits']),''])
    if 'paired_rows' in report:
        lines+=['## 逐例差异','']+[f"- {p['case_id']} / {p['repetition']}：{', '.join(p['changed_fields']) or '无差异'}" for p in report['paired_rows']]
    return '\n'.join(lines)+'\n'


def write_once(path,content):
    """独占创建，绝不覆盖已有评测产物。"""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:stream.write(content)
