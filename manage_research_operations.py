"""P5 显式排程/发布/回滚管理；默认不启用调度，不修改现有科研根任务配置。"""
import argparse
import json
from pathlib import Path
from evaluation.operations import Schedule,save_schedule,set_enabled,dispatch_once,initialize_channel,publish,rollback,choose_binding


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('init-tables')
    a=sub.add_parser('schedule');a.add_argument('file',type=Path)
    for name in ('enable','disable'):
        a=sub.add_parser(name);a.add_argument('id');a.add_argument('--actor',required=True)
    a=sub.add_parser('worker-once');a.add_argument('--worker-id',required=True);a.add_argument('--confirm-model-calls',action='store_true');a.add_argument('--checkpoints',type=Path,default=Path('outputs/experiments/scheduled.sqlite3'))
    a=sub.add_parser('init-channel');a.add_argument('id');a.add_argument('binding_id');a.add_argument('--actor',required=True)
    a=sub.add_parser('publish');a.add_argument('id');a.add_argument('decision_id');a.add_argument('--revision',type=int,required=True);a.add_argument('--actor',required=True);a.add_argument('--canary-percent',type=int,default=0);a.add_argument('--authorize-release',action='store_true')
    a=sub.add_parser('rollback');a.add_argument('id');a.add_argument('--revision',type=int,required=True);a.add_argument('--actor',required=True);a.add_argument('--authorize-release',action='store_true')
    a=sub.add_parser('route');a.add_argument('id');a.add_argument('new_task_id')
    args=p.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    if args.command=='init-tables':
        import os
        from storage.database import make_db
        from storage.research_operations import TABLES
        engine,_=make_db(os.environ['DATABASE_URL'])
        try:
            with engine.begin() as connection:
                for table in TABLES:table.create(connection,checkfirst=True)
        finally:engine.dispose()
        result={'tables_prepared':True,'scheduler_started':False}
    elif args.command=='schedule':
        spec=Schedule.model_validate(json.loads(args.file.read_text()))
        if spec.enabled:raise ValueError('新建排程必须禁用，随后显式enable。')
        result={'id':save_schedule(spec)}
    elif args.command in {'enable','disable'}:set_enabled(args.id,args.command=='enable',args.actor);result={'id':args.id,'enabled':args.command=='enable'}
    elif args.command=='worker-once':
        if not args.confirm_model_calls:raise ValueError('需要明确授权模型调用。')
        result=dispatch_once(args.worker_id,args.checkpoints)
    elif args.command=='init-channel':result=initialize_channel(args.id,args.binding_id,args.actor)
    elif args.command=='route':result=choose_binding(args.id,args.new_task_id)
    else:
        if not args.authorize_release:raise ValueError('生产渠道变更需要明确 --authorize-release。')
        result=publish(args.id,args.decision_id,args.revision,args.actor,args.canary_percent) if args.command=='publish' else rollback(args.id,args.revision,args.actor)
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'code':'RESEARCH_OPERATIONS_FAILED','error_type':type(exc).__name__,'message':'操作未完成；检查数据库、修订号、权限和验证依据。'},ensure_ascii=False));raise SystemExit(1)
