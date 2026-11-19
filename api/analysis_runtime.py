"""论文分析的 P0 薄适配层，默认关闭，历史任务不伪造快照。"""
import json
import os
import inspect
from sqlalchemy.exc import SQLAlchemyError

from agent.agent_version import AgentVersion, pinned_llm_factory
from agent.analysis_schema import PaperAnalysis
from agent import paper_analyzer
from agent.runtime_snapshot import RuntimeUnavailable, capture_runtime
from storage.runtime_versions import load_immutable, save_immutable


class RuntimeStorageUnavailable(RuntimeUnavailable):
    code = 'RUNTIME_STORAGE_UNAVAILABLE'


def enabled():
    value = os.getenv('ANALYSIS_RUNTIME_PINNING', 'false').lower().strip()
    if value not in {'true', 'false'}:
        raise ValueError('ANALYSIS_RUNTIME_PINNING 必须为 true 或 false。')
    return value == 'true'


def contracts():
    from agent.quote_selection import LayoutQuoteSelection, select_original_quotes
    return {'analysis': {'prompt': paper_analyzer.SYSTEM_PROMPT, 'schema': PaperAnalysis.model_json_schema(),
                         'message_builders': {name: inspect.getsource(function) for name, function in {
                             'analysis': paper_analyzer.analyze_paper,
                             'retry_feedback': paper_analyzer.analysis_retry_feedback,
                             'quote_selection': select_original_quotes}.items()},
                         'quote_selection_schema': LayoutQuoteSelection.model_json_schema()}}


def capture_binding(settings, request, *, database_url, db_factory):
    # 外层 batch_analysis 统一计数重试，实际 SDK 重试固定为零。
    runtime = capture_runtime(read_timeout=settings['timeout'], retries=0, trust_env=True)
    version = AgentVersion.capture(role='analysis', contracts=contracts(),
        tools={'settings': settings, 'request': request}, quote_protocol='literal-analysis-with-quote-selection-v1')
    engine, Session = db_factory(database_url)
    try:
        with Session.begin() as session:
            save_immutable(session, version)
            save_immutable(session, runtime)
    except SQLAlchemyError:
        raise RuntimeStorageUnavailable('P0 版本仓库不可用，请检查数据库连接，并先运行 python -m storage.init_runtime_versions 准备新表。') from None
    finally:
        engine.dispose()
    return version, runtime


def restore_binding(task, *, database_url, db_factory):
    if not task.get('agent_version_id') or not task.get('runtime_snapshot_id'):
        raise RuntimeUnavailable('这项历史任务没有运行快照，不能伪造恢复配置。')
    engine, Session = db_factory(database_url)
    try:
        with Session.begin() as session:
            version = load_immutable(session, task['agent_version_id'])
            runtime = load_immutable(session, task['runtime_snapshot_id'])
    except SQLAlchemyError:
        raise RuntimeStorageUnavailable('无法读取 P0 原版本，请检查版本表和数据库连接；不会使用当前配置替代。') from None
    finally:
        engine.dispose()
    if not isinstance(version, AgentVersion) or version.role != 'analysis':
        raise RuntimeUnavailable('原任务的分析版本不可用。')
    assert_binding(version, runtime)
    return version, runtime


def assert_binding(version, runtime):
    from agent.runtime_snapshot import resolve_runtime
    version.assert_available()
    if json.loads(version.contracts_json) != contracts():
        raise RuntimeUnavailable('内存中的分析 Prompt 或 Schema 已变化，不能混用原版本。')
    resolve_runtime(runtime)


def model_factory(binding):
    version, runtime = binding
    factory = pinned_llm_factory(runtime, version=version)

    def create(**kwargs):
        assert_binding(version, runtime)
        return factory(**kwargs)

    return create
