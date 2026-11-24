"""P3–P5 受控边界回归。最初延后测试，现已按用户要求执行fake检查。"""
from copy import deepcopy
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from agent.runtime_snapshot import RuntimeUnavailable
from evaluation import operations
from storage.research_operations import TABLES,EvaluationSchedule


@pytest.fixture
def operations_store(monkeypatch):
    engine=create_engine('sqlite://')
    for table in TABLES:table.create(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    def transaction(action):
        with Session.begin() as session:return action(session)
    monkeypatch.setattr(operations.provenance,'transaction',transaction)
    artifacts={'job':{'split':'development','max_calls':4,'total_seconds':30},'binding':{'registered':True}}
    monkeypatch.setattr(operations,'get',lambda identifier,kind=None:deepcopy(artifacts[identifier]))
    import evaluation.task_replay as replay
    monkeypatch.setattr(replay,'restore',lambda binding:None)
    yield Session,artifacts
    engine.dispose()


def test_disabled_schedule_no_dispatch_and_budget_conservatively_reserved(operations_store):
    Session,_=operations_store
    spec=operations.Schedule(id='schedule',job_id='job',interval_seconds=60,max_runs=2,max_total_calls=4,expires_at=9999999999.0,actor='operator')
    operations.save_schedule(spec)
    assert operations.claim_due('worker') is None
    operations.set_enabled('schedule',True,'operator')
    first=operations.claim_due('worker')
    assert first and operations.claim_due('other-worker') is None
    with Session.begin() as session:
        row=session.get(EvaluationSchedule,'schedule')
        assert row.payload['calls_reserved']==4 and row.payload['runs_reserved']==1
        row.lease_until=0;row.next_at=0
    # 过期旧轮次不退预算，下一轮不能通过崩溃绕过总调用上限。
    assert operations.claim_due('replacement-worker') is None


def test_schedule_never_accepts_holdout(operations_store):
    _,artifacts=operations_store;artifacts['job']['split']='holdout'
    spec=operations.Schedule(id='schedule',job_id='job',interval_seconds=60,max_runs=2,max_total_calls=10,expires_at=9999999999.0,actor='operator')
    with pytest.raises(RuntimeUnavailable,match='留出'):operations.save_schedule(spec)


def test_new_task_routes_stably_and_rollback_does_not_mutate_old_routing(operations_store):
    Session,artifacts=operations_store
    operations.initialize_channel('channel','binding','operator')
    first=operations.choose_binding('channel','stable-task')
    second=operations.choose_binding('channel','stable-task')
    assert first==second and first['binding_artifact_id']=='binding'
    from storage.research_operations import ReplayReleaseChannel
    with Session.begin() as session:
        row=session.get(ReplayReleaseChannel,'channel')
        row.payload=row.payload|{'candidate':'candidate-binding','canary_percent':100,'revision':2}
    artifacts['candidate-binding']={'registered':True}
    selected=operations.choose_binding('channel','new-task')
    assert selected['binding_artifact_id']=='candidate-binding'
    rolled=operations.rollback('channel',2,'operator')
    assert rolled['candidate'] is None and rolled['revision']==3
    assert operations.choose_binding('channel','another-new-task')['binding_artifact_id']=='binding'
    assert selected['binding_artifact_id']=='candidate-binding'
    with pytest.raises(RuntimeUnavailable):operations.rollback('channel',2,'operator')


def test_scheduler_cannot_dispatch_experiment_child_independently(operations_store):
    _,artifacts=operations_store;artifacts['job']['parent_experiment_id']='parent-experiment'
    spec=operations.Schedule(id='schedule',job_id='job',interval_seconds=60,max_runs=2,max_total_calls=10,expires_at=9999999999.0,actor='operator')
    with pytest.raises(RuntimeUnavailable,match='父预算'):operations.save_schedule(spec)
