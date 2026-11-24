"""模型驱动的离线Prompt演化。模型选工具；程序固定证据、预算、版本及发布边界。"""
from copy import deepcopy
import json
from pathlib import Path
import re
from time import time
from typing import Literal

from pydantic import Field, create_model, ValidationError
from agent.agent_version import AgentVersion
from agent.llm_runtime import analysis_llm
from agent.runtime_snapshot import RuntimeSnapshot, RuntimeUnavailable, fingerprint, resolve_runtime
from evaluation.harness_contracts import SKILLS, TOOLS, EVOLUTION_PROMPT, AUXILIARY_REVIEW_PROMPT
from evaluation.task_quality import Strict, Dataset, Outcome, score
from evaluation.task_replay import (Strategy, ReplayFinding, payload, materialize, response_schema,
                                    strategy_prompt, restore, derive_binding, build_run)
from evaluation.evolution import Candidate, GatePolicy, register_candidate
from evaluation.replay_jobs import permission_check
from storage.research_experiments import Checkpoints, ExperimentStopped, put, get


class Proposal(Strict):
    hypothesis: str = Field(min_length=10, max_length=3000)
    rationale: str = Field(min_length=10, max_length=6000)
    strategy: Strategy


class Action(Strict):
    tool: Literal['capabilities', 'read_case', 'read_material', 'read_skill', 'read_feedback',
                  'propose_candidate', 'run_probe', 'compare_probe', 'finish']
    reason: str = Field(min_length=1, max_length=3000)
    case_id: str | None = None
    material_id: str | None = None
    skill_id: Literal['evidence_grounding', 'experimental_conditions', 'numerical_comparison', 'conflicting_evidence'] | None = None
    candidate_id: str | None = None
    proposal: Proposal | None = None


class AuxiliaryReview(Strict):
    source_support: Literal['supported', 'contradicted', 'insufficient', 'uncertain']
    answer_completeness: Literal['answered', 'partial', 'unanswered', 'uncertain']
    reason: str = Field(min_length=1, max_length=6000)
    findings: list[ReplayFinding] = Field(max_length=12)


class AutonomousSpec(Strict):
    dataset_artifact_id: str
    baseline_binding_id: str
    controller_binding_id: str
    reviewer_binding_id: str
    policy_id: str
    max_calls: int = Field(default=24, ge=1, le=1000)
    max_steps: int = Field(default=8, ge=1, le=50)
    max_candidates: int = Field(default=1, ge=1, le=3)
    total_seconds: int = Field(default=1200, ge=10, le=86400)
    max_context_chars: int = Field(default=64000, ge=4000, le=200000)
    exercise_candidate: bool = False


def role_contract(role):
    return {'autonomous_evolution': {
        'prompt': EVOLUTION_PROMPT if role == 'controller' else AUXILIARY_REVIEW_PROMPT,
        'schema': (Action if role == 'controller' else AuxiliaryReview).model_json_schema(),
        'tools': TOOLS if role == 'controller' else {}, 'skills': SKILLS,
        'role': role, 'protocol': 'autonomous-prompt-evolution-v1'}}


def register_role(parent, role):
    """沿用显式基线模型配置，独立保存指令、schema和角色，不读取默认模型。"""
    _, runtime = restore(parent)
    version = AgentVersion.capture(role='research' if role == 'controller' else 'review',
        contracts=role_contract(role), tools={'autonomous_role': role},
        quote_protocol='task-replay-pointers-v1')
    from api import provenance
    from storage.runtime_versions import save_immutable
    provenance.transaction(lambda session: save_immutable(session, version))
    return put('autonomous_role_binding', {'role': role, 'version': version.model_dump(),
        'runtime': runtime.model_dump(), 'agent_version_id': version.id, 'runtime_snapshot_id': runtime.id})


def restore_role(binding, role):
    from api import provenance
    from storage.runtime_versions import load_immutable
    version = AgentVersion.model_validate(binding['version'])
    runtime = RuntimeSnapshot.model_validate(binding['runtime'])
    if (binding['role'] != role or version.id != binding['agent_version_id']
            or runtime.id != binding['runtime_snapshot_id']):
        raise RuntimeUnavailable('演化角色绑定不符。')
    recorded = provenance.transaction(lambda session: (load_immutable(session, version.id), load_immutable(session, runtime.id)))
    if recorded != (version, runtime) or json.loads(version.contracts_json) != role_contract(role):
        raise RuntimeUnavailable('演化角色指令或实际版本不可恢复。')
    version.assert_available()
    resolve_runtime(runtime)
    return version, runtime


def prepare(spec):
    dataset = Dataset.model_validate(get(spec.dataset_artifact_id, 'task_dataset'))
    permission_check(spec.dataset_artifact_id, dataset)
    if len(dataset.cases) > 20 or any(c.split != 'development' for c in dataset.cases):
        raise RuntimeUnavailable('自主演化只接受至多20条获准开发任务，不接受留出或回归材料。')
    parent = get(spec.baseline_binding_id, 'task_replay_binding')
    _, runtime = restore(parent)
    for key, role in ((spec.controller_binding_id, 'controller'), (spec.reviewer_binding_id, 'reviewer')):
        _, actual = restore_role(get(key, 'autonomous_role_binding'), role)
        if actual != runtime:
            raise RuntimeUnavailable('首版演化角色必须沿用固定基线运行配置。')
    policy = GatePolicy.model_validate(get(spec.policy_id, 'gate_policy'))
    if not policy.frozen_before_experiment:
        raise RuntimeUnavailable('实验前须冻结评分规则。')
    return put('autonomous_evolution_job', spec.model_dump())


class CallFailed(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_response(response, schema):
    if isinstance(response, dict) and 'parsed' in response:
        raw = response.get('raw')
        metadata = getattr(raw, 'response_metadata', {}) or {}
        if (metadata.get('finish_reason') in {'length', 'content_filter'}
                or (getattr(raw, 'additional_kwargs', {}) or {}).get('refusal')):
            raise CallFailed('AUTONOMOUS_RESPONSE_TRUNCATED')
        response = response['parsed']
    if hasattr(response, 'model_dump'):
        response = response.model_dump()
    return schema.model_validate(response)


def validate_patch(proposal, dataset):
    """阻止显式案例答案/编号灌入Prompt；不声称能识别所有语义层面的过拟合。"""
    instructions = proposal.strategy.instructions or ''
    for c in dataset.cases:
        if c.id.casefold() in instructions.casefold():
            raise ValueError('候选不能硬编码开发案例编号。')
        for m in c.materials:
            if any(x.casefold() in instructions.casefold() for x in (m.id, m.paper_key)):
                raise ValueError('候选不能硬编码材料身份。')
            # 跨换行规范化后检查连续原文，不让候选成为逐题答案表。
            text = ' '.join(m.text.split())
            candidate = ' '.join(instructions.split())
            if any(text[i:i+80] in candidate for i in range(0, max(0, len(text)-79), 20)):
                raise ValueError('候选不能复制具体原文作为答案。')


class EvolutionSession:
    def __init__(self, identifier, path):
        self.id = identifier
        self.spec = AutonomousSpec.model_validate(get(identifier, 'autonomous_evolution_job'))
        prepare(self.spec)
        self.dataset = Dataset.model_validate(get(self.spec.dataset_artifact_id, 'task_dataset'))
        self.parent = get(self.spec.baseline_binding_id, 'task_replay_binding')
        self.controller = get(self.spec.controller_binding_id, 'autonomous_role_binding')
        self.reviewer = get(self.spec.reviewer_binding_id, 'autonomous_role_binding')
        self.store = Checkpoints(path)
        self.store.create(identifier, self.spec.model_dump())
        self.token = self.store.acquire(identifier, self.spec.total_seconds + 120)
        if not self.token:
            raise RuntimeUnavailable('本轮自主演化已有执行者，不能重复调用。')

    def entries(self):
        return self.store.items(self.id)

    def call(self, key, binding, role, system, body, schema):
        if len(json.dumps(body, ensure_ascii=False)) > self.spec.max_context_chars:
            raise RuntimeUnavailable('实际模型输入超出本轮字符预算，未截断或调用。')
        digest = fingerprint({'system': system, 'body': {k:v for k,v in body.items() if k != 'budget'},
                              'schema': schema.model_json_schema()})
        item_id = 'call:' + key
        old = self.entries().get(item_id)
        if old:
            if old['outcome']['input_hash'] != digest:
                raise RuntimeUnavailable('恢复模型请求的固定输入已变化。')
            if old['pending']:
                raise CallFailed('AUTONOMOUS_ATTEMPT_INTERRUPTED')
            if old.get('error_code'):
                raise CallFailed(old['error_code'])
            return schema.model_validate(old['response'])
        permission_check(self.spec.dataset_artifact_id, self.dataset)
        if role == 'executor':
            _, runtime = restore(binding)
        else:
            _, runtime = restore_role(binding, role)
        identity = {'case_id': key, 'repetition': 1, 'input_hash': digest}
        pending = {'pending': True, 'attempt': 1, 'outcome': identity, 'role': role,
                   'runtime_snapshot_id': runtime.id, 'agent_version_id': binding['agent_version_id'],
                   'request': {'system': system, 'body': body, 'schema': schema.model_json_schema()},
                   'response': None, 'error_code': None}
        reservation = self.store.reserve(self.id, self.token, reservation=(item_id, pending))
        actual = RuntimeSnapshot.model_validate(runtime.model_dump() | {
            'read_timeout': min(runtime.read_timeout, max(0.001, reservation['deadline'] - time())), 'retries': 0})
        from api import provenance
        from storage.runtime_versions import save_immutable
        started = time()
        completed = {**pending, 'pending': False, 'runtime_snapshot_id': actual.id, 'invoke_started':False}
        try:
            provenance.transaction(lambda session: save_immutable(session, actual))
            self.store.check_deadline(self.id, self.token)
            with analysis_llm(runtime_snapshot=actual) as model:
                structured = model.with_structured_output(schema, method='json_schema', strict=True, include_raw=True)
                self.store.check_deadline(self.id, self.token)
                self.store.mark_started(self.id, self.token, item_id, actual.id)
                completed['invoke_started'] = True
                result = structured.invoke([('system', system), ('human', json.dumps(body, ensure_ascii=False))])
            permission_check(self.spec.dataset_artifact_id, self.dataset)
            self.store.check_deadline(self.id, self.token)
            completed['response'] = parse_response(result, schema).model_dump()
        except Exception as exc:
            completed['error_code'] = ('AUTONOMOUS_RESPONSE_INVALID' if isinstance(exc, ValidationError)
                                       else getattr(exc, 'code', 'AUTONOMOUS_MODEL_FAILED'))
            completed['error_type'] = type(exc).__name__
            from evaluation.claim_quality import error_info
            completed['safe_error'] = error_info(exc)[0]
            if completed['error_code'] == 'AUTONOMOUS_MODEL_FAILED' and completed['safe_error'].get('code') in {
                    'MODEL_TIMEOUT','MODEL_CONNECTION_ERROR','MODEL_HTTP_ERROR','MODEL_CONFIG_ERROR'}:
                completed['error_code'] = completed['safe_error']['code']
            if isinstance(exc, ValidationError):
                completed['validation_issues'] = [{'location':list(e['loc']), 'type':e['type'], 'message':e['msg']}
                    for e in exc.errors(include_input=False, include_url=False)[:5]]
                if role == 'executor' and any(set(e['loc']) & {'material_id','span_ids'}
                                              for e in exc.errors(include_input=False, include_url=False)):
                    completed['error_code'] = 'AUTONOMOUS_SOURCE_INVALID'
            completed['seconds'] = time() - started
            self.store.finish_item(self.id, self.token, item_id, completed)
            if isinstance(exc, (ExperimentStopped, RuntimeUnavailable)):
                raise
            raise CallFailed(completed['error_code']) from None
        completed['seconds'] = time() - started
        self.store.finish_item(self.id, self.token, item_id, completed)
        return schema.model_validate(completed['response'])

    def probe(self, candidate_id):
        cache_key = 'probe:' + candidate_id
        old = self.entries().get(cache_key)
        if old:
            return old
        if candidate_id == 'baseline':
            binding_id = self.spec.baseline_binding_id
        else:
            proposal = self.entries().get('candidate:' + candidate_id)
            if not proposal:
                raise ValueError('候选不属于本轮。')
            binding_id = proposal['binding_id']
        binding = get(binding_id, 'task_replay_binding')
        strategy = Strategy.model_validate(json.loads(binding['version']['tools_json'])['strategy'])
        outcomes, reviews = [], []
        for case in self.dataset.cases:
            case_key = 'case:' + fingerprint([candidate_id, case.id])
            saved = self.entries().get(case_key)
            if saved:
                outcomes.append(Outcome.model_validate(saved['outcome']))
                reviews.append(saved['review'])
                continue
            provided = payload(case, strategy)
            logical = 'executor:' + fingerprint([candidate_id, case.id])
            begin = time()
            try:
                answer = self.call(logical, binding, 'executor', strategy_prompt(strategy), provided, response_schema(provided))
                text, findings = materialize(answer.model_dump(), provided)
                outcome = Outcome(case_id=case.id, repetition=1, input_hash=case.input_hash,
                    status='succeeded', answer=text, findings=findings, human_judgments=[], calls=1)
            except (CallFailed, ValueError) as exc:
                if isinstance(exc, RuntimeUnavailable):
                    raise
                outcome = Outcome(case_id=case.id, repetition=1, input_hash=case.input_hash,
                    status='interrupted' if getattr(exc,'code',None)=='AUTONOMOUS_ATTEMPT_INTERRUPTED' else 'failed',
                    answer='', findings=[], human_judgments=[], calls=1,
                    error_code=getattr(exc, 'code', 'AUTONOMOUS_SOURCE_INVALID'))
            attempt = self.entries().get('call:' + logical)
            outcome.seconds = attempt.get('seconds') if attempt else None
            if attempt and attempt['pending']:
                outcome.calls = None
            review = {'status': 'not_requested', 'source_support': None, 'answer_completeness': None}
            if outcome.status == 'succeeded':
                # 独立角色、固定通用标准，无候选指令、人工要求或gold答案。
                finding_type = response_schema(provided).model_fields['findings'].annotation
                review_schema = create_model('EvolutionAuxiliaryReview', __base__=AuxiliaryReview,
                    findings=(finding_type, Field(max_length=12)))
                review_body = {'question': case.question, 'scope': case.scope, 'materials': provided['materials'],
                               'answer': outcome.answer, 'findings': [f.model_dump() for f in outcome.findings]}
                try:
                    assessed = self.call('review:' + fingerprint([candidate_id, case.id]), self.reviewer,
                        'reviewer', AUXILIARY_REVIEW_PROMPT, review_body, review_schema)
                    _, refs = materialize({'answer': assessed.reason,
                        'findings': [f.model_dump() for f in assessed.findings]}, provided)
                    if not refs and assessed.source_support != 'uncertain':
                        raise ValueError('确定的辅助判断必须提供原文依据。')
                    review = {'status': 'succeeded', **assessed.model_dump(),
                              'source_citations': [f.model_dump() for f in refs], 'is_human_gold': False}
                except (CallFailed, ValueError) as exc:
                    if isinstance(exc, RuntimeUnavailable):
                        raise
                    review = {'status': 'failed', 'error_code': getattr(exc, 'code', 'AUXILIARY_REVIEW_INVALID'),
                              'source_support': None, 'answer_completeness': None, 'is_human_gold': False}
            saved = {'outcome': outcome.model_dump(), 'review': review}
            self.store.save_item(self.id, self.token, case_key, saved)
            outcomes.append(outcome)
            reviews.append(review)
        run = build_run(self.dataset, binding, outcomes, 'development', 1,
                        'autonomous-probe:' + fingerprint([self.id, candidate_id]))
        metrics = score(self.dataset, run)['metrics']
        result = {'candidate_id': candidate_id, 'binding_id': binding_id,
            'run': run.model_dump(), 'metrics': metrics, 'auxiliary_reviews': reviews,
            'auxiliary_answered_and_supported': sum(r['status'] == 'succeeded'
                and r['source_support'] == 'supported' and r['answer_completeness'] == 'answered' for r in reviews),
            'auxiliary_denominator': len(self.dataset.cases),
            'auxiliary_failures': sum(r['status'] != 'succeeded' for r in reviews),
            'semantic_human_gold': 'not_collected', 'production_changed': False}
        result_id = put('autonomous_probe', result)
        result = {**result, 'artifact_id': result_id}
        self.store.save_item(self.id, self.token, cache_key, result)
        return result

    def allowed_probe(self, candidate_id):
        key = candidate_id or 'baseline'
        value = self.entries().get('probe:' + key)
        if value is None:
            raise ValueError('该版本尚未完成本轮probe。')
        return value

    def comparison(self, candidate_id):
        baseline, candidate = self.allowed_probe('baseline'), self.allowed_probe(candidate_id)
        def n(probe, key):
            return probe['metrics'][key]['numerator']
        source_safe = (n(candidate, 'execution_success_fraction') >= n(baseline, 'execution_success_fraction')
            and n(candidate, 'source_pass_fraction') >= n(baseline, 'source_pass_fraction'))
        delta = candidate['auxiliary_answered_and_supported'] - baseline['auxiliary_answered_and_supported']
        critical_errors = [o['case_id'] for o in candidate['run']['outcomes']
            if o['error_code'] in {'AUTONOMOUS_SOURCE_INVALID','SOURCE_OWNERSHIP_ERROR','FABRICATED_SOURCE','PERMISSION_DENIED'}]
        return {'candidate_id': candidate_id, 'task_denominator': len(self.dataset.cases),
            'execution_success_delta': n(candidate, 'execution_success_fraction') - n(baseline, 'execution_success_fraction'),
            'source_pass_delta': n(candidate, 'source_pass_fraction') - n(baseline, 'source_pass_fraction'),
            'auxiliary_supported_and_answered_delta': delta,
            'critical_deterministic_error_cases':critical_errors,
            'recommend_for_independent_validation': bool(not critical_errors and source_safe and delta > 0
                and candidate['auxiliary_failures'] == 0 and baseline['auxiliary_failures'] == 0),
            'production_decision': 'retain_baseline',
            'limitation': '辅助模型判断仅供开发诊断；不作为人工分数或晋级依据。'}

    def dispatch(self, action):
        args = action.model_dump(exclude={'tool', 'reason'}, exclude_none=True)
        allowed = {'capabilities': set(), 'read_case': {'case_id'}, 'read_material': {'case_id','material_id'},
            'read_skill': {'skill_id'}, 'read_feedback': {'candidate_id','case_id'}, 'propose_candidate': {'proposal'},
            'run_probe': {'candidate_id'}, 'compare_probe': {'candidate_id'}, 'finish': {'candidate_id'}}
        if set(args) - allowed[action.tool]:
            raise ValueError('该工具不能接收其他动作的参数。')
        if action.tool == 'capabilities':
            return {'tools': TOOLS, 'skills': SKILLS, 'arbitrary_file_or_shell_access': False}
        if action.tool in {'read_case','read_material'}:
            case = next((c for c in self.dataset.cases if c.id == action.case_id), None)
            if case is None:
                raise ValueError('案例不属于获准开发集合。')
            view = payload(case, Strategy())
            if action.tool == 'read_case':
                return {'case_id': case.id, 'question': case.question, 'scope': case.scope,
                        'materials': [{'material_id':m['material_id'], 'page_number':m['page_number']} for m in view['materials']]}
            material = next((m for m in view['materials'] if m['material_id'] == action.material_id), None)
            if material is None:
                raise ValueError('材料不属于对应案例。')
            return material
        if action.tool == 'read_skill':
            if action.skill_id not in SKILLS:
                raise ValueError('未知技能。')
            return {'skill_id': action.skill_id, **SKILLS[action.skill_id]}
        if action.tool == 'read_feedback':
            probe = self.allowed_probe(action.candidate_id)
            if action.case_id and action.case_id not in {c.id for c in self.dataset.cases}:
                raise ValueError('反馈案例不属于本轮开发集合。')
            return {'candidate_id': probe['candidate_id'], 'metrics': probe['metrics'],
                'auxiliary_summary': {'failed_or_missing':probe['auxiliary_failures'],
                    'supported_and_answered':probe['auxiliary_answered_and_supported'],
                    'denominator':probe['auxiliary_denominator']},
                'case_status_index':[{'case_id':o['case_id'],'status':o['status'],'error_code':o['error_code'],
                    'review_status':r['status'],'review_error_code':r.get('error_code'),
                    'auxiliary_source_support':r.get('source_support'),
                    'auxiliary_answer_completeness':r.get('answer_completeness')}
                    for o,r in zip(probe['run']['outcomes'],probe['auxiliary_reviews'])],
                'detail_scope':{'case_id':action.case_id,'full_task_denominator':len(self.dataset.cases),
                    'mode':'single_case' if action.case_id else 'summary',
                    'next_action':'需要详情时使用read_feedback并指定case_id；重复读取总览不会展开更多内容。'},
                'cases': [{'case_id':o['case_id'], 'status':o['status'], 'error_code':o['error_code'],
                    'answer':o['answer'], 'findings':o['findings'], 'auxiliary_review':r}
                    for o,r in zip(probe['run']['outcomes'], probe['auxiliary_reviews'])
                    if o['case_id']==action.case_id],
                'human_gold': 'not_provided'}
        if action.tool == 'propose_candidate':
            if action.proposal is None:
                raise ValueError('缺少候选。')
            validate_patch(action.proposal, self.dataset)
            baseline_strategy = Strategy.model_validate(json.loads(self.parent['version']['tools_json'])['strategy'])
            if any(getattr(action.proposal.strategy, key) != getattr(baseline_strategy, key)
                   for key in ('max_input_chars','max_output_tokens')):
                raise ValueError('候选必须沿用基线输入输出上限，不能扩大自己的资源预算。')
            if action.proposal.strategy == baseline_strategy:
                raise ValueError('候选必须实际改变通用指令、profile或技能，不能原样重复基线。')
            used_skills = {v['action'].get('skill_id') for k,v in self.entries().items()
                           if k.startswith('step:') and v['action']['tool'] == 'read_skill' and v['observation'].get('ok')}
            if not set(action.proposal.strategy.skills) <= used_skills:
                raise ValueError('先读取候选将使用的技能。')
            candidate = Candidate(**action.proposal.model_dump(), parent_binding_id=self.spec.baseline_binding_id,
                                  author='autonomous-evolution-controller')
            identifier = 'experiment_' + fingerprint(['evolution_candidate', candidate.model_dump()])[:48]
            if 'candidate:' + identifier in self.entries():
                return self.entries()['candidate:' + identifier]
            if sum(k.startswith('candidate:') for k in self.entries()) >= self.spec.max_candidates:
                raise ValueError('候选数量上限已达到。')
            actual_id = register_candidate(candidate)
            child_id = put('task_replay_binding', derive_binding(self.parent, candidate.strategy))
            result = {'candidate_id': actual_id, 'binding_id': child_id, 'proposal': candidate.model_dump()}
            self.store.save_item(self.id, self.token, 'candidate:' + actual_id, result)
            return result
        if action.tool == 'run_probe':
            if not action.candidate_id or action.candidate_id == 'baseline':
                raise ValueError('基线已由程序执行；请选择本轮候选。')
            entries = self.entries()
            if 'candidate:' + action.candidate_id not in entries:
                raise ValueError('请选择本轮已登记候选。')
            if 'probe:' + action.candidate_id not in entries:
                budget = self.store.create(self.id, self.spec.model_dump())
                # 本次控制器请求已预扣；候选回答/评审之外保留一次finish请求。
                if (self.spec.max_steps - sum(k.startswith('step:') for k in entries) < 2
                        or self.spec.max_calls - budget['calls_reserved'] < 2 * len(self.dataset.cases) + 1):
                    raise ExperimentStopped('AUTONOMOUS_FINISH_BUDGET_INSUFFICIENT')
            probe = self.probe(action.candidate_id)
            return {k:probe[k] for k in ('candidate_id','artifact_id','metrics','auxiliary_answered_and_supported','auxiliary_failures')}
        if action.tool == 'compare_probe':
            if not action.candidate_id or action.candidate_id == 'baseline':
                raise ValueError('请选择完成probe的候选。')
            return self.comparison(action.candidate_id)
        if action.tool == 'finish':
            viewed_baseline = any(k.startswith('step:') and v['action']['tool'] == 'read_feedback'
                and v['observation'].get('ok') and v['observation']['result'].get('candidate_id') == 'baseline'
                for k,v in self.entries().items())
            if not viewed_baseline:
                raise ValueError('先用read_feedback读取candidate_id为baseline的真实逐例反馈；当前消息未展开不等于材料不存在。')
            pending = [v['candidate_id'] for k,v in self.entries().items()
                if k.startswith('candidate:') and 'probe:' + v['candidate_id'] not in self.entries()]
            if pending:
                raise ValueError('已提出的候选尚未执行run_probe，不能宣告完成。')
            if self.spec.exercise_candidate and not any(k.startswith('probe:') and v['candidate_id'] != 'baseline'
                                                       for k,v in self.entries().items()):
                raise ValueError('本轮是候选链路验收，须提出并执行一个通用候选的run_probe；允许无收益，但不能只完成基线。')
            if action.candidate_id:
                self.allowed_probe(action.candidate_id)
            return {'finished': True, 'model_recommendation': action.candidate_id,
                    'reason': action.reason, 'production_decision': 'retain_baseline'}
        raise ValueError('不可用工具。')

    def finish_state(self):
        baseline = self.allowed_probe('baseline')
        if any(o['status'] != 'succeeded' for o in baseline['run']['outcomes']):
            return 'partial', 'baseline_execution_incomplete'
        if baseline['auxiliary_failures']:
            return 'partial', 'baseline_auxiliary_review_incomplete'
        probes = [v for k,v in self.entries().items() if k.startswith('probe:') and v['candidate_id'] != 'baseline']
        if any(p['auxiliary_failures'] for p in probes):
            return 'partial', 'candidate_auxiliary_review_incomplete'
        return 'completed', 'model_finished' if probes else 'model_no_change_after_diagnosis'

    def controller_input(self, history):
        budget = self.store.create(self.id, self.spec.model_dump())
        entries = self.entries()
        reads = [v['action'] for k,v in entries.items() if k.startswith('step:')
                 and v['action']['tool'] == 'read_feedback' and v['observation'].get('ok')]
        candidates = [v['candidate_id'] for k,v in entries.items() if k.startswith('candidate:')]
        body = {'objective': '改善可复用的科研回答Prompt和技能选择，执行真实工具与开发对照；不发布生产。',
            'experiment_mode': 'candidate_path_validation' if self.spec.exercise_candidate else 'autonomous_optimization',
            'exercise_candidate':self.spec.exercise_candidate,
            'baseline_strategy': Strategy.model_validate(json.loads(self.parent['version']['tools_json'])['strategy']).model_dump(),
            'tools': TOOLS, 'skills': {k:{'revision':v['revision'],'purpose':v['purpose']} for k,v in SKILLS.items()},
            'cases': [{'case_id':c.id,'question':c.question,'scope':c.scope} for c in self.dataset.cases],
            'baseline_probe_available': True, 'history': history,
            'progress': {'baseline_feedback_read':any((r.get('candidate_id') or 'baseline') == 'baseline' for r in reads),
                'feedback_reads':[{'candidate_id':r.get('candidate_id') or 'baseline','case_id':r.get('case_id')} for r in reads],
                'candidate_ids':candidates,
                'probed_candidate_ids':[c for c in candidates if 'probe:' + c in entries]},
            'budget': {'calls_remaining':self.spec.max_calls-budget['calls_reserved'],
                'steps_remaining':self.spec.max_steps-len(history), 'candidate_limit':self.spec.max_candidates,
                'candidate_probe_calls':2 * len(self.dataset.cases), 'finish_calls_reserved_required':1,
                'guidance':'每次选择工具也消耗一次模型调用；run_probe后至少留一步和一次调用给finish。比较可由程序保存，预算紧张时直接finish。'}}
        # 保留编号/动作和最新反馈；裁剪仅影响改进者上下文，不改变待评任务或证据。
        for entry in body['history']:
            rendered = json.dumps(entry['observation'], ensure_ascii=False)
            if len(rendered) > 12000:
                result = entry['observation'].get('result', {})
                entry['observation'] = {'ok':entry['observation'].get('ok'), 'details_omitted':True,
                    'summary':{k:result[k] for k in ('candidate_id','metrics','auxiliary_summary','case_status_index','detail_scope') if k in result},
                    'cases':[{'case_id':c['case_id'],'status':c['status'],'error_code':c['error_code'],
                        'answer_preview':(c.get('answer') or '')[:4000],
                        'answer_chars':len(c.get('answer') or ''),
                        'auxiliary_review':{k:c.get('auxiliary_review',{}).get(k) for k in
                            ('status','error_code','source_support','answer_completeness')},
                        'review_reason_preview':(c.get('auxiliary_review',{}).get('reason') or '')[:1000]}
                        for c in result.get('cases',[])],
                    'retrieval':{'tool':entry['action']['tool'],
                        'case_id':entry['action'].get('case_id'), 'material_id':entry['action'].get('material_id')},
                    'message':'完整观察已保存在轨迹中。总览按case_id展开；原文按read_case/read_material定位。不要反复读取同一总览。'}
        while len(json.dumps(body, ensure_ascii=False)) > self.spec.max_context_chars and body['history']:
            body['history'].pop(0)
        if len(json.dumps(body, ensure_ascii=False)) > self.spec.max_context_chars:
            raise RuntimeUnavailable('控制器固定输入超出预算，未截断开发任务。')
        return body

    def run(self):
        state, stop_reason = 'partial', 'step_limit'
        try:
            self.store.update(self.id, self.token, state='running')
            self.probe('baseline')
            history = []
            for step in range(self.spec.max_steps):
                saved = self.entries().get('step:' + str(step))
                if saved:
                    history.append(deepcopy(saved))
                    if saved['action']['tool'] == 'finish' and saved['observation'].get('ok'):
                        state, stop_reason = self.finish_state()
                        break
                    continue
                body = self.controller_input(deepcopy(history))
                try:
                    action = self.call('controller:' + str(step), self.controller, 'controller', EVOLUTION_PROMPT, body, Action)
                except CallFailed as exc:
                    if exc.code in {'AUTONOMOUS_RESPONSE_INVALID','AUTONOMOUS_RESPONSE_TRUNCATED'}:
                        attempt = self.entries().get('call:controller:' + str(step), {})
                        saved = {'step':step, 'action':{'tool':'invalid_model_response','reason':exc.code},
                            'observation':{'ok':False,'code':exc.code,
                                'validation_issues':attempt.get('validation_issues', []),
                                'message':'本步没有执行工具；修正结构或缩短响应，下一步仍扣共同预算。'}}
                        self.store.save_item(self.id, self.token, 'step:' + str(step), saved)
                        history.append(deepcopy(saved))
                        continue
                    stop_reason = exc.code
                    break
                try:
                    observation = {'ok': True, 'result': self.dispatch(action)}
                except (ExperimentStopped, RuntimeUnavailable):
                    raise
                except ValueError as exc:
                    observation = {'ok': False, 'code':'AUTONOMOUS_TOOL_REJECTED', 'message':str(exc)[:500]}
                saved = {'step':step,'action':action.model_dump(),'observation':observation}
                self.store.save_item(self.id, self.token, 'step:' + str(step), saved)
                history.append(deepcopy(saved))
                if action.tool == 'finish' and observation['ok']:
                    state, stop_reason = self.finish_state()
                    break
            self.store.update(self.id, self.token, state=state, stop_reason=stop_reason)
        except Exception as exc:
            stop_reason = getattr(exc, 'code', 'AUTONOMOUS_EVOLUTION_FAILED')
            self.store.update(self.id, self.token, state='partial', stop_reason=stop_reason)
        finally:
            self.store.release(self.id, self.token)
        entries = self.entries()
        comparisons = [self.comparison(v['candidate_id']) for k,v in entries.items()
                       if k.startswith('probe:') and v['candidate_id'] != 'baseline']
        recommended = [c['candidate_id'] for c in comparisons if c['recommend_for_independent_validation']]
        snapshot = self.store.create(self.id, self.spec.model_dump())
        baseline = entries.get('probe:baseline')
        candidate_count = sum(k.startswith('candidate:') for k in entries)
        usage = {}
        for key, value in entries.items():
            if not key.startswith('call:'):
                continue
            role = usage.setdefault(value['role'], {'reserved_requests':0,'known_started_requests':0,
                'interrupted_attempts':0,'known_seconds':0.0,'tokens':None,'cost':None})
            role['reserved_requests'] += 1
            role['known_started_requests'] += bool(value.get('invoke_started'))
            role['interrupted_attempts'] += bool(value['pending'])
            if value.get('seconds') is not None:
                role['known_seconds'] += value['seconds']
        result = {'job_id':self.id,'checkpoint':snapshot,'comparisons':comparisons,
            'experiment_mode':'candidate_path_validation' if self.spec.exercise_candidate else 'autonomous_optimization',
            'workflow_status': ('candidate_comparison_available' if comparisons else
                'candidate_not_evaluated' if candidate_count else 'baseline_only'),
            'candidate_count':candidate_count, 'comparison_count':len(comparisons),
            'baseline_auxiliary_failures':baseline['auxiliary_failures'] if baseline else None,
            'usage_by_role':usage,
            'candidates_for_independent_validation':recommended, 'production_decision':'retain_baseline',
            'production_changed':False,'human_gold':'not_collected','independent_validation':'not_performed',
            'trajectory':[v for k,v in sorted(entries.items(), key=lambda item: int(item[0][5:]) if item[0].startswith('step:') else -1) if k.startswith('step:')],
            'probe_artifact_ids':[v['artifact_id'] for k,v in entries.items() if k.startswith('probe:')],
            'attempts':{k:v for k,v in entries.items() if k.startswith('call:')},
            'limits':['模型辅助评审不是人工真值；开发收益不支持自动晋级。',
                      '候选是隔离的Prompt和技能配置，不执行任意代码或替换生产Prompt。',
                      '标识/数值/原文复制检查不能证明候选不存在所有语义过拟合。']}
        return put('autonomous_evolution_result', result)


def execute(identifier, path):
    return EvolutionSession(identifier, path).run()
