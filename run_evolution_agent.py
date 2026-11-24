"""一条命令启动受限的LLM Prompt演化；真实调用仅在显式授权后执行。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent


def save_once(path, value):
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8')) != value:
            raise ValueError('已有归档内容不同，不能覆盖。')
        return
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def test_database(output, dataset):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema
    from agent.runtime_snapshot import fingerprint, RuntimeUnavailable
    value = os.getenv('TEST_DATABASE_URL')
    if not value:
        raise RuntimeUnavailable('请配置TEST_DATABASE_URL，不使用生产DATABASE_URL。')
    url = make_url(value)
    if url.get_backend_name() != 'postgresql' or url.database != 'researchpilot_test':
        raise RuntimeUnavailable('本入口只允许researchpilot_test PostgreSQL测试库。')
    schema = 'rp_evoagent_' + fingerprint([str(output), dataset.sha256])[:16]
    engine = create_engine(url, connect_args={'connect_timeout': 5})
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema, if_not_exists=True))
    finally:
        engine.dispose()
    scoped = url.update_query_dict({'options': (str(url.query.get('options', '')) + ' -csearch_path=' + schema).strip()})
    os.environ['DATABASE_URL'] = scoped.render_as_string(hide_password=False)
    from storage.runtime_versions import TABLES
    from storage.coverage_reports import TABLES as COVERAGE_TABLES
    from storage.database import make_db
    engine, _ = make_db(os.environ['DATABASE_URL'])
    try:
        with engine.begin() as connection:
            for table in [*TABLES, *COVERAGE_TABLES]:
                table.create(connection, checkfirst=True)
    finally:
        engine.dispose()
    return schema


def run(args, output):
    from dotenv import load_dotenv
    from sqlalchemy.exc import SQLAlchemyError
    from agent.runtime_snapshot import fingerprint, RuntimeUnavailable
    from evaluation.task_quality import Dataset
    from evaluation.task_replay import Strategy, register
    from evaluation.evolution import GatePolicy
    from evaluation.evolution_agent import AutonomousSpec, register_role, prepare, execute
    from storage.research_experiments import put, get
    load_dotenv(ROOT / '.env')
    previous_database = os.environ.get('DATABASE_URL')
    stage = 'validate_inputs'
    attempt = {'id': uuid4().hex, 'started_at': datetime.now(timezone.utc).isoformat(),
               'production_changed': False, 'human_gold_created': False}
    invoked = False
    try:
        dataset = Dataset.model_validate(json.loads(args.dataset.read_text(encoding='utf-8')))
        if any(c.split != 'development' or not c.permission.allowed for c in dataset.cases) or len(dataset.cases) > 20:
            raise RuntimeUnavailable('仅接受至多20条获授权开发任务；不要提供留出答案。')
        request = {'dataset_sha256': dataset.sha256, 'max_calls':args.max_calls, 'max_steps':args.max_steps,
            'max_candidates':args.max_candidates, 'total_seconds':args.total_seconds, 'timeout':args.timeout,
            'max_output_tokens':args.tokens,'exercise_candidate':args.exercise_candidate}
        # 所有数值在接触DB/LLM前使用实际契约校验。
        AutonomousSpec(dataset_artifact_id='pending', baseline_binding_id='pending', controller_binding_id='pending',
            reviewer_binding_id='pending', policy_id='pending', **{k:request[k] for k in ('max_calls','max_steps','max_candidates','total_seconds','exercise_candidate')})
        if not 0 < args.timeout <= args.total_seconds:
            raise ValueError('单次时限必须为正且不超过总期限。')
        strategy = Strategy(max_output_tokens=args.tokens)
        config_path = output / 'registration.json'
        existing = json.loads(config_path.read_text()) if config_path.exists() else None
        if existing and existing['request'] != request:
            raise RuntimeUnavailable('恢复必须沿用原材料、预算和配置；变更需使用新的输出目录。')
        stage = 'connect_test_database'
        schema = test_database(output, dataset)
        stage = 'register_configuration'
        if existing:
            identifier = existing['job_id']
            prepare(AutonomousSpec.model_validate(get(identifier, 'autonomous_evolution_job')))
        else:
            binding = register(strategy, args.timeout)
            parent = put('task_replay_binding', binding)
            controller, reviewer = register_role(binding, 'controller'), register_role(binding, 'reviewer')
            dataset_id = put('task_dataset', dataset.model_dump())
            policy = put('gate_policy', GatePolicy(revision='autonomous-development-v1', author='fixed-program-policy',
                frozen_before_experiment=True).model_dump())
            spec = AutonomousSpec(dataset_artifact_id=dataset_id, baseline_binding_id=parent,
                controller_binding_id=controller, reviewer_binding_id=reviewer, policy_id=policy,
                **{k:request[k] for k in ('max_calls','max_steps','max_candidates','total_seconds','exercise_candidate')})
            identifier = prepare(spec)
            save_once(config_path, {'job_id':identifier, 'request':request, 'spec':spec.model_dump(),
                                   'schema':schema, 'dataset_file':str(args.dataset.resolve())})
        attempt.update(job_id=identifier, schema=schema)
        print(json.dumps({'stage':'registered', **attempt}, ensure_ascii=False), flush=True)
        if args.prepare_only:
            attempt.update(status='prepared', model_calls=0)
            return 0
        stage = 'execute_autonomous_loop'
        invoked = True
        result_id = execute(identifier, output / 'checkpoints.sqlite3')
        result = get(result_id, 'autonomous_evolution_result')
        save_once(output / (result_id + '.json'), result)
        for probe_id in result['probe_artifact_ids']:
            save_once(output / (probe_id + '.json'), get(probe_id, 'autonomous_probe'))
        trajectory = output / ('trajectory_' + result_id + '.jsonl')
        if not trajectory.exists():
            with trajectory.open('x', encoding='utf-8') as stream:
                for event in result['trajectory']:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        attempt.update(result_id=result_id, status=result['checkpoint']['state'],
            experiment_mode=result['experiment_mode'],
            workflow_status=result['workflow_status'], candidate_count=result['candidate_count'],
            comparison_count=result['comparison_count'], baseline_auxiliary_failures=result['baseline_auxiliary_failures'],
            calls_reserved=result['checkpoint']['calls_reserved'], stop_reason=result['checkpoint']['stop_reason'],
            production_decision=result['production_decision'],
            candidates_for_independent_validation=result['candidates_for_independent_validation'])
        save_once(output / ('result_' + result_id + '.json'), attempt)
        (output / 'result.json').write_text(json.dumps({'latest_result_id':result_id, **attempt}, ensure_ascii=False, indent=2)+'\n')
        response = output / ('response_' + result_id + '.md')
        if not response.exists():
            response.write_text('# Prompt自主演化开发记录\n\n'
                + '状态：'+attempt['status']+'；停止原因：'+str(attempt['stop_reason'])+'。\n\n'
                + '阶段：'+attempt['workflow_status']+'；候选：'+str(attempt['candidate_count'])
                + '；对照：'+str(attempt['comparison_count'])+'；基线辅助评审失败：'+str(attempt['baseline_auxiliary_failures'])+'。\n\n'
                + '共同预扣模型请求：'+str(attempt['calls_reserved'])+'。\n\n'
                + '控制器、任务回答和辅助评审的请求、工具选择与结果均保留在同目录JSON/JSONL及检查点。\n\n'
                + '待独立验证的候选：'+json.dumps(attempt['candidates_for_independent_validation'])+'。\n\n'
                + '辅助评审不是人工标准答案；生产基线保持不变。\n', encoding='utf-8')
        print(json.dumps(attempt, ensure_ascii=False))
        return 0 if attempt['status'] == 'completed' else 2
    except Exception as exc:
        attempt.update(status='stopped', stage=stage,
            code='TEST_DATABASE_UNAVAILABLE' if isinstance(exc, SQLAlchemyError) else getattr(exc, 'code','AUTONOMOUS_EVOLUTION_INPUT_OR_RUNTIME_ERROR'),
            error_type=type(exc).__name__, model_calls=0 if not invoked else None,
            message=str(exc) if isinstance(exc, RuntimeUnavailable) else '请核对本次阶段、测试库及有效配置，已保留原结果。')
        print(json.dumps(attempt, ensure_ascii=False))
        return 1
    finally:
        if previous_database is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = previous_database
        attempt['finished_at'] = datetime.now(timezone.utc).isoformat()
        logs = output / 'attempts'
        logs.mkdir(exist_ok=True)
        save_once(logs / (attempt['id'] + '.json'), attempt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare-only', action='store_true')
    group.add_argument('--confirm-model-calls', action='store_true')
    parser.add_argument('--max-calls', type=int, default=24)
    parser.add_argument('--max-steps', type=int, default=8)
    parser.add_argument('--max-candidates', type=int, default=1)
    parser.add_argument('--total-seconds', type=int, default=1200)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--tokens', type=int, default=1200)
    parser.add_argument('--exercise-candidate', action='store_true',
        help='候选链路验收：至少执行一个实际不同的通用候选；不要求收益、不改变评分或发布规则')
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    if not output.is_relative_to((ROOT / 'outputs').resolve()) or output == (ROOT / 'outputs').resolve():
        parser.error('输出目录必须是项目outputs下的专用子目录。')
    output.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (output / '.launcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'code':'AUTONOMOUS_EVOLUTION_ALREADY_RUNNING'}))
            return 1
        return run(args, output)


if __name__ == '__main__':
    raise SystemExit(main())
