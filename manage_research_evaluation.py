"""P3 显式材料导出、实际回放与人工标注归档；默认无自动后台执行。"""
import argparse
import json
from pathlib import Path
from evaluation.task_quality import Dataset, Run, Judgment, Outcome, fingerprint
from storage.research_experiments import put,get


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('export');a.add_argument('spec',type=Path)
    a=sub.add_parser('register-dataset');a.add_argument('file',type=Path)
    a=sub.add_parser('register-binding');a.add_argument('--profile',default='standard');a.add_argument('--timeout',type=float,default=120);a.add_argument('--tokens',type=int,default=4000)
    a=sub.add_parser('prepare');a.add_argument('spec',type=Path);a.add_argument('--channel');a.add_argument('--new-task-id')
    a=sub.add_parser('execute');a.add_argument('job_id');a.add_argument('--checkpoints',type=Path,default=Path('outputs/experiments/jobs.sqlite3'));a.add_argument('--confirm-model-calls',action='store_true')
    a=sub.add_parser('read');a.add_argument('artifact_id');a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('annotate');a.add_argument('result_id');a.add_argument('judgments',type=Path)
    a=sub.add_parser('withdraw');a.add_argument('dataset_id');a.add_argument('--reason',required=True);a.add_argument('--actor',required=True)
    args=p.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    if args.command=='export':
        from evaluation.task_export import ExportCase,export_dataset
        value=json.loads(args.spec.read_text());result=export_dataset(value['id'],value['revision'],value['annotation_version'],[ExportCase.model_validate(v) for v in value['cases']])
    elif args.command=='register-dataset':
        value=Dataset.model_validate(json.loads(args.file.read_text()))
        if any(not c.permission.allowed for c in value.cases):raise ValueError('含未授权材料。')
        result={'dataset_artifact_id':put('task_dataset',value.model_dump())}
    elif args.command=='register-binding':
        from evaluation.task_replay import Strategy,register
        result={'binding_artifact_id':put('task_replay_binding',register(Strategy(profile=args.profile,max_output_tokens=args.tokens),args.timeout))}
    elif args.command=='prepare':
        from evaluation.replay_jobs import ReplayJob,prepare
        value=json.loads(args.spec.read_text())
        if args.channel:
            if not args.new_task_id:raise ValueError('灰度路由需要稳定的新任务ID。')
            from evaluation.operations import choose_binding
            route=choose_binding(args.channel,args.new_task_id)
            value.update(binding_artifact_id=route['binding_artifact_id'],release_route=route)
        result={'job_id':prepare(ReplayJob.model_validate(value))[0]}
    elif args.command=='execute':
        if not args.confirm_model_calls:raise ValueError('执行需显式 --confirm-model-calls。')
        from evaluation.replay_jobs import run_job
        result={'result_artifact_id':run_job(args.job_id,args.checkpoints)}
    elif args.command=='read':
        from evaluation.task_quality import write_once
        write_once(args.output,json.dumps(get(args.artifact_id),ensure_ascii=False,indent=2)+'\n');result={'output':str(args.output)}
    elif args.command=='withdraw':
        result={'withdrawal_id':put('evaluation_withdrawal',{'dataset_artifact_id':args.dataset_id,'reason':args.reason,'actor':args.actor})}
    else:
        value=get(args.result_id,'task_replay_result');run=Run.model_validate(value['run']);annotations=json.loads(args.judgments.read_text())
        if annotations['run_fingerprint']!=fingerprint(run.model_dump()):raise ValueError('标注不属于实际运行结果。')
        by_key={(o.case_id,o.repetition):o for o in run.outcomes};seen=set()
        for row in annotations['items']:
            key=(row['case_id'],row['repetition'])
            if key in seen or key not in by_key:raise ValueError('重复或未知标注。')
            seen.add(key);by_key[key]=Outcome.model_validate(by_key[key].model_dump()|{'human_judgments':[Judgment.model_validate(v).model_dump() for v in row['judgments']]})
        annotated=Run.model_validate(run.model_dump()|{'outcomes':[v.model_dump() for v in by_key.values()]})
        from evaluation.task_quality import check_run
        source_job=get(value['job_id'],'task_replay_job')
        dataset=Dataset.model_validate(get(source_job['dataset_artifact_id'],'task_dataset'))
        check_run(dataset,annotated,allow_holdout=True)
        result={'annotation_id':put('task_run_annotations',{'source_result_id':args.result_id,'source_run_fingerprint':fingerprint(run.model_dump()),'run':annotated.model_dump()})}
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'code':'EVALUATION_MANAGEMENT_FAILED','error_type':type(exc).__name__,'message':'未完成，请检查授权、配置、固定版本和输入契约；未自动替换生产版本。'},ensure_ascii=False));raise SystemExit(1)
