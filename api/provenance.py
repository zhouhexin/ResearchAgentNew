"""交互、计划修订、任务关联的薄适配；不采集历史未知配置。"""
import os
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from api.analysis_runtime import RuntimeStorageUnavailable
from storage.database import make_db
from storage.research_provenance import InteractionRecord, save_once, save_revision, read_revision

FIELDS = ('interaction_id', 'root_task_id', 'parent_task_id', 'plan_id', 'plan_revision', 'plan_snapshot_id',
          'research_goal', 'research_questions', 'research_scope', 'runtime_bindings', 'plan_step',
          'execution_id', 'search_run_id')


def transaction(action):
    engine, Session = make_db(os.environ['DATABASE_URL'])
    try:
        with Session.begin() as session:
            return action(session)
    except SQLAlchemyError:
        raise RuntimeStorageUnavailable('P0 溯源仓库不可用，请检查连接并执行 python -m storage.init_runtime_versions。') from None
    finally:
        engine.dispose()


def decorate(task, *, source=None, parent_task_id=None):
    """只为新固定配置任务记录来源；被复用的任务不改写父任务。"""
    from api import tasks
    source = source or tasks.CURRENT_TASK.get() or {}
    inherited = {key: source[key] for key in FIELDS if key in source}
    if inherited.get('root_task_id'):
        inherited['parent_task_id'] = parent_task_id or source.get('task_id') or source.get('id') or source.get('source_task_id')
    inherited.update(task)
    task = inherited
    if not task.get('runtime_bindings') and not task.get('interaction_id'):
        return task
    task['parent_task_id'] = parent_task_id or source.get('task_id') or source.get('source_task_id') or (
        source.get('id') if source.get('type') else task.get('parent_task_id'))
    task.setdefault('token_usage', None)
    task.setdefault('cost', None)
    if not task.get('interaction_id'):
        task['interaction_id'] = 'interaction_' + uuid4().hex
        task['root_task_id'] = task['id']
        interaction = {'id': task['interaction_id'], 'root_task_id': task['id'],
                       'research_goal': task.get('research_goal') or task.get('topic'),
                       'research_questions': task.get('research_questions', []),
                       'scope': task.get('research_scope'), 'created_at': task['created_at'],
                       'evaluation_allowed': False, 'evaluation_permission_source': None}
        transaction(lambda session: save_once(session, InteractionRecord, interaction['id'], interaction))
    return task


def fields(source):
    return {key: source[key] for key in FIELDS if key in source}


def plan_fields(plan):
    return {**fields(plan), 'plan_id': plan['id'], 'plan_revision': plan['revision'],
            'plan_snapshot_id': plan.get('plan_snapshot_id'), 'research_goal': plan['goal'],
            'research_questions': plan['plan']['research_questions'], 'research_scope': plan['plan']['scope']}


def freeze_plan(plan):
    if not plan.get('interaction_id'):
        return None
    return transaction(lambda session: save_revision(session, plan))


def load_plan_revision(identifier):
    return transaction(lambda session: read_revision(session, identifier))
