"""P0 多角色运行清单。根任务一次捕获，子任务仅按编号恢复。"""
from contextlib import contextmanager
from functools import wraps
import inspect
import json
import os

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from agent.agent_version import AgentVersion
from agent.runtime_snapshot import RuntimeSnapshot, RuntimeUnavailable, canonical, capture_runtime, resolve_runtime
from api.analysis_runtime import RuntimeStorageUnavailable
from storage.database import make_db
from storage.runtime_versions import load_immutable, save_immutable

OPERATIONS = {'planning': 'research', 'search': 'research', 'analysis': 'analysis',
              'claim_review': 'review', 'questions': 'research', 'review': 'analysis', 'coverage': 'review',
              'report_review': 'review'}


def enabled():
    value = os.getenv('RESEARCH_RUNTIME_PINNING', 'false').strip().lower()
    if value not in {'true', 'false'}:
        raise ValueError('RESEARCH_RUNTIME_PINNING 必须为 true 或 false。')
    return value == 'true'


def contracts(operation):
    from agent import paper_analyzer, research_planner, research_search, claim_reference_verifier, question_coverage
    from review import questions, synthesis
    from review import report_review
    from api.analysis_runtime import contracts as analysis_contracts
    if operation == 'analysis':
        return analysis_contracts()
    module, prompt, schema = {
        'planning': (research_planner, research_planner.PLANNING_PROMPT, research_planner.ResearchPlan),
        'search': (research_search, research_search.SCREEN_PROMPT, research_search.ScreeningBatch),
        'claim_review': (claim_reference_verifier, claim_reference_verifier.REFERENCE_REVIEW_PROMPT,
                         claim_reference_verifier.ReferenceClaimReview),
        'questions': (questions, questions.QUESTION_PROMPT, questions.QuestionSuggestions),
        'review': (synthesis, synthesis.SYSTEM, synthesis.ReviewDraft),
        'coverage': (question_coverage, question_coverage.PROMPT, question_coverage.CoverageDraft),
        'report_review': (report_review, report_review.PROMPT, report_review.ReferenceClaimReview),
    }[operation]
    return {operation: {'prompt': prompt, 'schema': schema.model_json_schema(),
                        'message_builders': inspect.getsource(module)}}


def _positive(name, default):
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f'{name} 必须为正整数。')
    return value


def capture_bundle():
    from api.analyses import model_settings, batch_settings
    from tools.execution_config import capture as capture_tools
    from review.synthesis import review_output_tokens
    from agent.question_coverage import POLICY as COVERAGE_POLICY
    common = model_settings()
    max_chars, batch_retries = batch_settings()
    settings = {operation: dict(common) for operation in OPERATIONS}
    settings['planning']['output_tokens'] = _positive('RESEARCH_PLAN_MAX_OUTPUT_TOKENS', 6000)
    settings['search']['output_tokens'] = 6000
    settings['claim_review'].update(output_tokens=_positive('CLAIM_REVIEW_MAX_OUTPUT_TOKENS', 2000),
                                    max_input_chars=_positive('CLAIM_REVIEW_MAX_INPUT_CHARS', 24000))
    settings['review']['output_tokens'] = review_output_tokens()
    settings['coverage'].update(output_tokens=_positive('COVERAGE_MAX_OUTPUT_TOKENS', 6000),
                                max_input_chars=_positive('COVERAGE_MAX_INPUT_CHARS', 60000))
    settings['report_review'].update(retries=0, output_tokens=_positive('REPORT_REVIEW_MAX_OUTPUT_TOKENS', 2000),
                                    max_input_chars=_positive('REPORT_REVIEW_MAX_INPUT_CHARS', 60000))
    settings['analysis'].update(retries=0, batch_size=2, batch_max_chars=max_chars,
                                batch_retries=batch_retries, scope='full')
    tool_config = capture_tools()
    versions, runtimes = {}, {}
    # 共用一次代码/依赖清单，角色各自保存实际 Prompt/Schema 和调用预算。
    base = AgentVersion.capture(role='research', contracts=contracts('planning'), tools={}, quote_protocol='p0-v1')
    for operation, role in OPERATIONS.items():
        versions[operation] = base.model_copy(update={
            'role': role, 'contracts_json': canonical(contracts(operation)),
            'tools_json': canonical({'operation': operation, 'settings': settings[operation], 'search': tool_config}),
            'quote_protocol': {'claim_review': 'references-v2', 'report_review': 'report-references-v1',
                               'coverage': COVERAGE_POLICY}.get(operation, 'p0-' + operation + '-v1')})
        runtimes[operation] = capture_runtime(read_timeout=settings[operation]['timeout'],
            retries=settings[operation]['retries'], max_output_tokens=settings[operation].get('output_tokens'))
    engine, Session = make_db(os.environ['DATABASE_URL'])
    try:
        with Session.begin() as session:
            for operation in OPERATIONS:
                save_immutable(session, versions[operation])
                save_immutable(session, runtimes[operation])
    except SQLAlchemyError:
        raise RuntimeStorageUnavailable('P0 版本仓库不可用，请检查连接并执行 python -m storage.init_runtime_versions。') from None
    finally:
        engine.dispose()
    return {operation: {'agent_version_id': versions[operation].id, 'runtime_snapshot_id': runtimes[operation].id}
            for operation in OPERATIONS}


def bundle_for(source=None):
    from api import tasks
    if source and source.get('runtime_bindings'):
        return source['runtime_bindings']
    current = tasks.CURRENT_TASK.get()
    if current and current.get('runtime_bindings'):
        return current['runtime_bindings']
    return capture_bundle() if enabled() else None


def restore(bundle, operation):
    if not bundle or operation not in bundle:
        raise RuntimeUnavailable('原角色运行清单缺失，不能使用当前默认配置替代。')
    refs = bundle[operation]
    engine, Session = make_db(os.environ['DATABASE_URL'])
    try:
        with Session.begin() as session:
            version = load_immutable(session, refs['agent_version_id'])
            runtime = load_immutable(session, refs['runtime_snapshot_id'])
    except SQLAlchemyError:
        raise RuntimeStorageUnavailable('原运行清单不可读取，请检查 P0 版本表和数据库。') from None
    finally:
        engine.dispose()
    if not isinstance(version, AgentVersion) or not isinstance(runtime, RuntimeSnapshot) or version.role != OPERATIONS[operation]:
        raise RuntimeUnavailable('角色运行版本不匹配。')
    binding = Binding(version, runtime, operation)
    binding.check()
    return binding


class Binding:
    def __init__(self, version, runtime, operation):
        self.version, self.runtime, self.operation = version, runtime, operation
        self.tools = json.loads(version.tools_json)
        self.settings = self.tools['settings']

    @property
    def metadata(self):
        return {'agent_version_id': self.version.id, 'runtime_snapshot_id': self.runtime.id,
                'model_name': self.runtime.model, 'http_backend': self.runtime.backend,
                'read_timeout': self.runtime.read_timeout}

    def check(self):
        self.version.assert_available()
        if json.loads(self.version.contracts_json) != contracts(self.operation):
            raise RuntimeUnavailable('原角色 Prompt 或 Schema 已改变。')
        resolve_runtime(self.runtime)
        from tools.execution_config import check
        check(self.tools['search'])

    @contextmanager
    def llm(self):
        from agent.llm_runtime import analysis_llm
        self.check()
        with analysis_llm(runtime_snapshot=self.runtime) as model:
            yield GuardedModel(model, self.check)


class GuardedModel:
    def __init__(self, model, check):
        self.model, self.check = model, check

    def with_structured_output(self, *args, **kwargs):
        self.check()
        return GuardedModel(self.model.with_structured_output(*args, **kwargs), self.check)

    def invoke(self, *args, **kwargs):
        self.check()
        result = self.model.invoke(*args, **kwargs)
        self.check()
        return result


def for_task(task_id, operation):
    from api.tasks import get_task
    bundle = get_task(task_id).get('runtime_bindings')
    return restore(bundle, operation) if bundle else None


def metadata(bundle, operation):
    return dict(runtime_bindings=bundle, **restore(bundle, operation).metadata) if bundle else {}


def unavailable(exc):
    return HTTPException(503 if isinstance(exc, RuntimeStorageUnavailable) else 409,
                         detail={'code': exc.code, 'message': str(exc), 'retryable': False})


def task_worker(function):
    @wraps(function)
    def wrapped(first, *args, **kwargs):
        from api.tasks import task_scope
        identifier = first['task_id'] if isinstance(first, dict) else first
        with task_scope(identifier):
            return function(first, *args, **kwargs)
    return wrapped
