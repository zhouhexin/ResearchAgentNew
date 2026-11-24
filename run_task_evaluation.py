"""P3 固定任务集离线结果核验与成对比较；不加载 .env，不调用模型。"""
import argparse
import json
from pathlib import Path
from evaluation.task_quality import Dataset, Run, InvalidEvaluation, load, score, compare, markdown, write_once


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--run',type=Path,help='待核验结果文件，不是执行模型的命令')
    parser.add_argument('--baseline',type=Path,help='与 --run 成对比较的基线结果文件')
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--allow-holdout',action='store_true',help='明确读取留出结果，仅离线评测，不供候选优化')
    parser.add_argument('--output-dir',type=Path,help='创建全新报告目录，不覆盖历史')
    args=parser.parse_args(argv)
    try:
        dataset=load(args.dataset,Dataset)
        if args.check_only:
            if args.run or args.baseline or args.output_dir:raise InvalidEvaluation('--check-only 不与结果或输出参数混用。')
            print(json.dumps({'dataset_id':dataset.id,'revision':dataset.revision,'dataset_sha256':dataset.sha256,
                'case_count':len(dataset.cases),'permission_denied_count':sum(not c.permission.allowed for c in dataset.cases),
                'model_calls':0,'status':'dataset_schema_valid'},ensure_ascii=False));return 0
        if not args.run:raise InvalidEvaluation('请指定 --run 或 --check-only。')
        run=load(args.run,Run)
        result=compare(dataset,load(args.baseline,Run),run,args.allow_holdout) if args.baseline else score(dataset,run,args.allow_holdout)
        if args.output_dir:
            args.output_dir.mkdir(parents=True,exist_ok=False)
            write_once(args.output_dir/'report.json',json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            write_once(args.output_dir/'report.md',markdown(result))
        print(json.dumps({'status':'offline_comparison_completed' if args.baseline else 'offline_check_completed','model_calls':0,
            'dataset_sha256':dataset.sha256,'output_dir':str(args.output_dir) if args.output_dir else None,
            'metrics':result.get('metrics'),'baseline_metrics':result.get('left',{}).get('metrics'),
            'candidate_metrics':result.get('right',{}).get('metrics'),'changed_pairs':result.get('changed_pairs')},ensure_ascii=False));return 0
    except (InvalidEvaluation,OSError) as exc:
        print(json.dumps({'code':'TASK_EVALUATION_INVALID','message':str(exc) if isinstance(exc,InvalidEvaluation) else '无法读取文件或创建新目录；已有报告不会覆盖。'},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
