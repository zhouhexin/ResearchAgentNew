"""P2 业务验收快照。程序检查与人工声明分开，无模型调用和评测授权。"""
import json
from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from api import coverage, provenance, research_execution, reviews, runtime_bindings, tasks
from api.analysis_runtime import RuntimeStorageUnavailable

router=APIRouter(prefix='/api/v1',tags=['研究流程验收记录'])
MANUAL_KEYS={'source_quotes','semantic_judgments','pdf_navigation','export_files'}


class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    coverage_id:str|None=Field(default=None,max_length=100)
    report_id:str|None=Field(default=None,max_length=100)
    report_review_id:str|None=Field(default=None,max_length=100)
    report_replay_id:str|None=Field(default=None,max_length=100)


class Attestation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    key:Literal['source_quotes','semantic_judgments','pdf_navigation','export_files']
    verdict:Literal['passed','failed','not_checked']
    reason:str=Field(max_length=2000)

    @model_validator(mode='after')
    def valid(self):
        self.reason=self.reason.strip()
        if self.verdict!='not_checked' and not self.reason:raise ValueError('通过或失败的人工声明必须填写核对依据。')
        return self


class SaveAcceptance(Selection):
    expected_fingerprint:str=Field(pattern='^[a-f0-9]{64}$')
    reviewer:str=Field(min_length=1,max_length=100)
    manual:list[Attestation]=Field(min_length=4,max_length=4)

    @model_validator(mode='after')
    def valid(self):
        self.reviewer=self.reviewer.strip()
        if not self.reviewer or {v.key for v in self.manual}!=MANUAL_KEYS:raise ValueError('请填写复核人及四项不重复的人工检查。')
        return self


def inspect_execution(plan_id,execution_id,selection):
    with tasks.connection() as db:
        research_execution.initialize(db)
        run=research_execution.public_execution(db,research_execution.read_execution(db,plan_id,execution_id))
    checks=[];artifacts=[]
    def check(key,action):
        try:
            value=action()
            checks.append({'key':key,'status':'passed','detail':'当前输入核验通过。'})
            return value
        except RuntimeStorageUnavailable:raise
        except RuntimeUnavailable as exc:
            checks.append({'key':key,'status':'failed','detail':str(exc),'code':exc.code})
        except HTTPException as exc:
            if exc.status_code>=500:raise
            checks.append({'key':key,'status':'failed','detail':'指定产物缺失、失效或不属于本执行。','code':'ACCEPTANCE_INPUT_INVALID'})
        except ValueError:
            checks.append({'key':key,'status':'failed','detail':'输入内容或引用未通过核验。','code':'ACCEPTANCE_VALIDATION_FAILED'})
    def validate_inputs():
        if not run['papers'] or any(p['analysis'].get('status')!='succeeded' or not p['analysis'].get('analysis_id') for p in run['papers']):
            raise HTTPException(422)
        body=reviews.ReviewSelection(paper_ids=[p['paper_id'] for p in run['papers']],analysis_ids=[p['analysis']['analysis_id'] for p in run['papers']])
        snapshots,_=reviews.prepare_selection(body)
        return snapshots
    checks.append({'key':'execution_completed','status':'passed' if run['status']=='succeeded' else 'failed',
        'detail':'执行状态：'+run['status']+'；不代表语义正确。'})
    snapshots=check('analysis_sources',validate_inputs)
    if snapshots:
        for snapshot in snapshots:
            artifacts.append({'kind':'analysis','id':snapshot['id'],'fingerprint':fingerprint(reviews.load_analysis(snapshot['id'])),
                'paper_id':snapshot['paper']['id'],'document_id':snapshot['document']['id'],
                'document_sha256':snapshot['document']['content_sha256'],'selected_chunks':snapshot['document']['selected_chunks'],
                'total_chunks':snapshot['document']['total_chunks'],'scope':snapshot.get('batch_summary',{}).get('scope','unknown')})
    def runtimes():
        bundle=run.get('runtime_bindings')
        if not bundle:raise RuntimeUnavailable('历史执行没有固定角色清单，配置未知。')
        for operation in bundle:runtime_bindings.restore(bundle,operation)
    check('runtime_replay_available',runtimes)
    def plan_revision():
        saved=provenance.load_plan_revision(run['plan_snapshot_id'])
        if saved['id']!=plan_id or saved['revision']!=run['revision'] or saved['plan']!=run['plan']:
            raise RuntimeUnavailable('执行与固定计划修订不符。')
        artifacts.append({'kind':'plan_revision','id':run['plan_snapshot_id'],'fingerprint':fingerprint(saved)})
    if run.get('plan_snapshot_id'):check('plan_revision',plan_revision)
    review_ids=[p['review']['review_id'] for p in run['papers'] if p.get('review',{}).get('review_id')]
    def linked_reviews():
        if run.get('review_selection_id'):
            from api.execution_review_links import verify_linked_run
            verify_linked_run(run)
        material=coverage.review_material(review_ids,[p['analysis'].get('analysis_id') for p in run['papers']])
        for item in material:
            artifacts.append({'kind':'claim_review','id':item['id'],'analysis_id':item['analysis_id'],'fingerprint':fingerprint(item)})
    if review_ids:check('associated_reviews',linked_reviews)
    else:checks.append({'key':'associated_reviews','status':'not_selected','detail':'没有关联评审，未验证。'})
    def cover():
        saved=coverage.read(selection.coverage_id)
        if saved.get('kind')!='result' or saved.get('execution_id')!=execution_id or saved.get('plan_id')!=plan_id:raise HTTPException(422)
        coverage.verify_request(coverage.read(saved['request_id']))
        artifacts.append({'kind':'coverage','id':saved['id'],'fingerprint':fingerprint(saved)})
    def report():
        saved=reviews.get_review(selection.report_id)
        context=saved.get('research_context') or {}
        if context.get('execution_id')!=execution_id or context.get('plan_id')!=plan_id or not saved.get('is_current'):raise HTTPException(422)
        if saved['analysis_ids']!=[p['analysis'].get('analysis_id') for p in run['papers']]:raise HTTPException(422)
        artifacts.append({'kind':'report','id':saved['id'],'fingerprint':fingerprint(saved)})
    for key,identifier,action in [('coverage',selection.coverage_id,cover),('report',selection.report_id,report)]:
        if identifier:check(key,action)
        else:checks.append({'key':key,'status':'not_selected','detail':'未选择产物，未验证。'})
    def report_check(identifier,kind):
        from api import report_checks
        if not selection.report_id:raise HTTPException(422)
        value=report_checks.detail(selection.report_id,identifier)
        if not value['source_unchanged'] or not value['complete'] or value['check_kind']!=kind:raise HTTPException(422)
        report_checks.verify(coverage.read(value['request_id']))
        artifacts.append({'kind':kind,'id':identifier,'fingerprint':fingerprint(value)})
    for key,identifier,kind in [('report_independent_review',selection.report_review_id,'independent_review'),
                                ('report_original_replay',selection.report_replay_id,'original_replay')]:
        if identifier:check(key,lambda identifier=identifier,kind=kind:report_check(identifier,kind))
    payload={'plan_id':plan_id,'execution_id':execution_id,'plan_revision':run['revision'],
        'plan_fingerprint':fingerprint(run['plan']),'questions':run['plan']['research_questions'],
        'selection':selection.model_dump(),'execution_status':run['status'],'checks':checks,'artifacts':artifacts,
        'runtime_bindings':run.get('runtime_bindings'),'missing_review_analysis_ids':[p['analysis'].get('analysis_id') for p in run['papers'] if not p.get('review',{}).get('review_id')],
        'limits':['程序校验通过不代表语义正确、真实模型可用或人工验收完成。','运行版本可用检查仅说明当前可恢复，模型输出不承诺逐字一致。','未选择的产物和未关联的评审不算通过。','人工声明由填写人提供，身份未认证；本记录不授予评测或优化使用权限。']}
    return {**payload,'fingerprint':fingerprint(payload)}


@router.post('/agent/plans/{plan_id}/executions/{execution_id}/acceptance/inspect')
def inspect_route(plan_id:str,execution_id:str,body:Selection):
    try:return inspect_execution(plan_id,execution_id,body)
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


@router.post('/agent/plans/{plan_id}/executions/{execution_id}/acceptance',status_code=201)
def save(plan_id:str,execution_id:str,body:SaveAcceptance):
    try:
        selection=Selection(**body.model_dump(include=set(Selection.model_fields)))
        inspected=inspect_execution(plan_id,execution_id,selection)
        if inspected['fingerprint']!=body.expected_fingerprint:raise HTTPException(409,detail='检查后材料或状态已变化，请刷新检查并重新核对，不保存过期声明。')
        record={'id':'acceptance_'+uuid4().hex,'kind':'execution_acceptance','created_at':tasks.now(),
            'plan_id':plan_id,'execution_id':execution_id,'inspection':inspected,
            'manual':[v.model_dump() for v in body.manual],'reviewer':body.reviewer,
            'reviewer_identity_verified':False,'evaluation_allowed':False}
        coverage.store(record)
        return record
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


@router.get('/agent/plans/{plan_id}/executions/{execution_id}/acceptance')
def history(plan_id:str,execution_id:str):
    from sqlalchemy import select
    from storage.coverage_reports import CoverageArtifact
    with tasks.connection() as db:
        research_execution.initialize(db);research_execution.read_execution(db,plan_id,execution_id)
    # 查询不可变业务记录，不创建新的任务或模型调用。
    def query(session):
        return [row.payload for row in session.scalars(select(CoverageArtifact).where(CoverageArtifact.id.like('acceptance_%')))]
    result=[]
    for envelope in provenance.transaction(query):
        value=envelope['content']
        if value.get('kind')=='execution_acceptance' and value.get('plan_id')==plan_id and value.get('execution_id')==execution_id:
            if fingerprint(value)!=envelope['content_sha256']:raise runtime_bindings.unavailable(RuntimeUnavailable('验收记录内容校验失败。'))
            result.append({'id':value['id'],'created_at':value['created_at'],'reviewer':value['reviewer']})
    return {'items':sorted(result,key=lambda r:r['created_at'],reverse=True)}


@router.get('/acceptance/{identifier}')
def read(identifier:str):
    try:
        value=coverage.read(identifier)
        if value.get('kind')!='execution_acceptance':raise HTTPException(404,detail='验收记录不存在。')
        try:current=inspect_execution(value['plan_id'],value['execution_id'],Selection(**value['inspection']['selection']))
        except HTTPException as exc:
            if exc.status_code>=500:raise
            return {**value,'inputs_unchanged':False,'current_inspection':None}
        return {**value,'inputs_unchanged':current['fingerprint']==value['inspection']['fingerprint'],'current_inspection':current}
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


@router.get('/acceptance/{identifier}/files/{format}')
def download(identifier:str,format:str):
    if format not in {'json','md'}:raise HTTPException(422,detail='仅支持JSON或Markdown。')
    record=read(identifier)
    if format=='json':content=json.dumps(record,ensure_ascii=False,indent=2)
    else:
        content='\n'.join(['# 研究流程验收记录','',f"记录：{record['id']}",f"执行：{record['execution_id']}",f"复核人（自述）：{record['reviewer']}",
            f"保存后输入未变化：{record['inputs_unchanged']}",'','程序检查与人工声明分开；不自动认定阶段完成，不授权评测用途。','',
            '## 保存时程序检查','',*[f"- {c['key']}：{c['status']}；{c['detail']}" for c in record['inspection']['checks']],
            '','## 人工声明','',*[f"- {v['key']}：{v['verdict']}；{v['reason']}" for v in record['manual']],
            '','## 固定产物','',*[f"- {a['kind']}：{a['id']}；SHA256：{a['fingerprint']}" for a in record['inspection']['artifacts']]])+'\n'
    return Response(content,media_type='application/json' if format=='json' else 'text/markdown',
        headers={'Content-Disposition':f'attachment; filename="research_acceptance.{format}"','X-Content-Type-Options':'nosniff'})
