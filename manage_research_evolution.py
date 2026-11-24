"""P4 离线数据策略实验入口。只准备任务，不自动运行模型/发布。"""
import argparse
import json
from pathlib import Path
from storage.research_experiments import put,get
from evaluation.evolution import Candidate, GatePolicy, register_candidate, failure_hypotheses,evaluate_pair


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('candidate');a.add_argument('file',type=Path)
    a=sub.add_parser('freeze-policy');a.add_argument('file',type=Path)
    a=sub.add_parser('diagnose');a.add_argument('result_id')
    a=sub.add_parser('prepare-experiment');a.add_argument('file',type=Path)
    a=sub.add_parser('compare');a.add_argument('experiment_id');a.add_argument('baseline_result');a.add_argument('candidate_result')
    a=sub.add_parser('execute');a.add_argument('experiment_id');a.add_argument('--checkpoints',type=Path,default=Path('outputs/experiments/evolution.sqlite3'));a.add_argument('--confirm-model-calls',action='store_true')
    args=p.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    if args.command=='candidate':result=register_candidate(Candidate.model_validate(json.loads(args.file.read_text())))
    elif args.command=='freeze-policy':result=put('gate_policy',GatePolicy.model_validate(json.loads(args.file.read_text())).model_dump())
    elif args.command=='diagnose':result=failure_hypotheses(args.result_id)
    elif args.command=='compare':
        experiment=get(args.experiment_id,'evolution_experiment')
        result=evaluate_pair(args.baseline_result,args.candidate_result,experiment['policy_id'],args.experiment_id)
    elif args.command=='execute':
        if not args.confirm_model_calls:raise ValueError('需要显式授权模型调用。')
        from evaluation.evolution import execute_experiment
        result=execute_experiment(args.experiment_id,args.checkpoints)
    else:
        value=json.loads(args.file.read_text())
        allowed={'baseline_binding_id','candidate_ids','policy_id','max_rounds','dataset_artifact_id','max_calls','total_seconds','author'}
        if set(value)!=allowed or not value['candidate_ids'] or len(value['candidate_ids'])>5 or len(set(value['candidate_ids']))!=len(value['candidate_ids']) or not 1<=value['max_rounds']<=3 or not 1<=value['max_calls']<=1000 or not 10<=value['total_seconds']<=86400:raise ValueError('实验范围/预算无效。')
        from evaluation.task_quality import Dataset
        dataset=Dataset.model_validate(get(value['dataset_artifact_id'],'task_dataset'))
        if any(c.split!='development' for c in dataset.cases):raise ValueError('实验开发包不得含回归或留出答案。')
        get(value['baseline_binding_id'],'task_replay_binding');get(value['policy_id'],'gate_policy')
        bindings=[]
        from evaluation.task_replay import derive_binding
        for identifier in value['candidate_ids']:
            candidate=Candidate.model_validate(get(identifier,'evolution_candidate'))
            if candidate.parent_binding_id!=value['baseline_binding_id']:raise ValueError('候选不属于同一基线。')
            bindings.append(put('task_replay_binding',derive_binding(get(value['baseline_binding_id'],'task_replay_binding'),candidate.strategy)))
        value['candidate_binding_ids']=bindings
        result=put('evolution_experiment',value)
    print(json.dumps({'artifact_id':result},ensure_ascii=False));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'code':'EVOLUTION_FAILED','error_type':type(exc).__name__,'production_changed':False}));raise SystemExit(1)
