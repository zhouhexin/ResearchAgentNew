"""独立运行语义证据评审，与人工标注对照；默认首批，可显式选择开发集合。"""
import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from agent.claim_verifier import POLICY_VERSION, ReviewPrecheckError
from agent.llm_runtime import (analysis_llm, model_endpoint, model_http_backend, model_name,
                               model_provider, validate_model_configuration)
from evaluation.claim_quality import (atomic_json, compare_results, load_annotations, load_dataset,
                                      inside, render_comparison, run_reviews)

ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = ROOT/'outputs/evaluations/claim_quality/two_papers_v1'


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    result.add_argument('--annotations', type=Path, help='人工导出 JSON；默认用材料包当前确认的归档，绝不发送给评审模型')
    selection = result.add_mutually_exclusive_group()
    selection.add_argument('--sample-id', action='append', help='显式选择样本；默认只评审材料包首批 8 条，不运行预留验证集')
    selection.add_argument('--split', choices=['development', 'validation_reserved'], help='显式选择整个集合；development 为 HarnessDev 的 15 条')
    result.add_argument('--output-dir', type=Path, help='结果目录，默认材料包 review_runs/；相同配置自动继续未完成项')
    result.add_argument('--timeout', type=float, help='单次请求秒数，默认 LLM_READ_TIMEOUT')
    result.add_argument('--max-output-tokens', type=int, help='默认 CLAIM_REVIEW_MAX_OUTPUT_TOKENS=2000；Codex 输出由 CLI 管理')
    result.add_argument('--max-input-chars', type=int, default=24000, help='单结论材料字符预算；超出则拒绝，不截断')
    result.add_argument('--no-proxy', action='store_true', help='本次调用不使用环境/系统代理，不修改配置')
    result.add_argument('--check-only', action='store_true', help='只检查 PDF 来源、输入和人工标注，不调用模型')
    result.add_argument('--quote-protocol', choices=['verbatim-v1', 'references-v2'], default='verbatim-v1',
                        help='默认保留冻结评测的 v1；references-v2 使用后端原文编号，独立运行，不混用旧评测结果')
    return result


def runtime_config(args):
    timeout = args.timeout if args.timeout is not None else float(os.getenv('LLM_READ_TIMEOUT', '400'))
    tokens = args.max_output_tokens if args.max_output_tokens is not None else int(os.getenv('CLAIM_REVIEW_MAX_OUTPUT_TOKENS', '2000'))
    retries = int(os.getenv('LLM_MAX_RETRIES', '0'))
    method = os.getenv('STRUCTURED_OUTPUT_METHOD', 'json_schema')
    if not math.isfinite(timeout) or timeout <= 0 or tokens < 1 or retries < 0 or args.max_input_chars < 1:
        raise ValueError('超时、输入输出预算必须为正数，重试次数不能为负数。')
    if method not in {'json_schema', 'function_calling'}:
        raise ValueError('结构化方式须为 json_schema 或 function_calling。')
    endpoint = model_endpoint()
    parsed = urlsplit(endpoint)
    # 不记录端点用户信息、查询参数或完整路径；哈希仍能隔离配置变化。
    public_endpoint = parsed.scheme + '://' + (parsed.hostname or '')
    config = {'policy_version': POLICY_VERSION, 'provider': model_provider(), 'model_name': model_name(),
              'http_backend': model_http_backend(), 'endpoint': public_endpoint,
              'endpoint_sha256': sha256(endpoint.encode()).hexdigest(),
              'timeout': timeout, 'max_output_tokens': tokens, 'sdk_retries': retries,
              'trust_env': not args.no_proxy, 'structured_method': method,
              'runtime_options_sha256': sha256(json.dumps({key: os.getenv(key, '') for key in
                  ('LLM_ENABLE_THINKING', 'LLM_MAX_OUTPUT_TOKENS', 'CODEX_USE_SYSTEM_PROXY')}, sort_keys=True).encode()).hexdigest()}
    if args.quote_protocol != 'verbatim-v1':
        config['quote_protocol'] = args.quote_protocol
    return config


def main(argv=None):
    args = parser().parse_args(argv)
    load_dotenv(ROOT/'.env')
    manifest, items = load_dataset(args.dataset, args.sample_id, split=args.split, max_input_chars=args.max_input_chars)
    annotation_path = args.annotations
    if annotation_path is None:
        progress_path = args.dataset/'annotation_progress.json'
        if progress_path.exists():
            progress_record = json.loads(progress_path.read_text())
            by_split = progress_record.get('split_annotations', {})
            annotation_path = inside(args.dataset.resolve(), by_split.get(args.split) or progress_record['source_annotations'])
        else:
            annotation_path = args.dataset/'annotations/pilot_round_001.json'
    annotations = load_annotations(annotation_path, manifest, items)
    if args.sample_id is None and len(annotations) != len(items):
        raise ReviewPrecheckError('所选样本尚未全部人工确认，未自动扩大送评范围')
    if args.check_only:
        print(json.dumps({'status': 'precheck_passed', 'selected_samples': len(items),
                          'human_labelled': len(annotations), 'model_calls': 0,
                          'selected_ids': [item['sample_id'] for item in items]}, ensure_ascii=False))
        return 0
    config = runtime_config(args)
    validate_model_configuration()
    quote_mode = '原文编号' if config.get('quote_protocol') == 'references-v2' else '逐字引文（冻结评测协议）'
    print(f"辅助语义评审：{len(items)} 条；人工标签仅用于独立评分；模型 {config['model_name']}；连接方式 {config['http_backend']}；引用方式：{quote_mode}。", flush=True)

    @contextmanager
    def factory():
        with analysis_llm(read_timeout=config['timeout'], retries=config['sdk_retries'],
                          trust_env=config['trust_env'], max_output_tokens=config['max_output_tokens']) as llm:
            yield llm

    def progress(number, total, key, status):
        labels = {'requesting': '正在请求评审模型', 'succeeded': '评审已保存', 'resumed': '复用已核验评审',
                  'recovered': '已恢复保存的模型输出并核验原文（无新模型调用）',
                  'failed': '模型评审未完成', 'precheck_failed': '原文前置核验失败', 'not_attempted': '本项尚未请求'}
        print(f"第 {number}/{total} 条 · {key} · {labels[status]}", flush=True)

    run_dir, results = run_reviews(items, factory, args.output_dir or args.dataset/'review_runs', config,
                                  method=config['structured_method'], max_input_chars=args.max_input_chars,
                                  progress=progress)
    report = compare_results(items, results, annotations)
    if args.split == 'validation_reserved':
        report['scope'] = 'held_out_paper_validation'
        report['limitations'][0] = '仅为单篇留出论文的小规模验证，人工为单人标注，不代表跨领域或生产可靠性。'
    report.update(dataset_id=manifest['dataset_id'], reviewer_inputs_sha256=manifest['reviewer_inputs_sha256'],
                  annotation_file_sha256=sha256(annotation_path.read_bytes()).hexdigest(),
                  annotation_source=str(annotation_path.resolve()), model_config=config)
    annotation_metadata = json.loads(annotation_path.read_text())
    if annotation_metadata.get('evaluation_context'):
        report['annotation_context'] = annotation_metadata['evaluation_context']
    if args.split == 'validation_reserved' and report.get('annotation_context', {}).get('model_influenced_labels'):
        report['scope'] = 'held_out_paper_post_model_adjudication'
        report['limitations'][0] = '本轮为查看模型输出后人工复核的标签一致率；原始留出验证标签和改标前评分另行保留。'
    report['selection'] = {'split': args.split, 'sample_ids_explicit': args.sample_id is not None,
                           'default_pilot': args.split is None and args.sample_id is None}
    # 人工答案更新可重新评分；原有对照按标注文件哈希保留，不丢失上一轮。
    atomic_json(run_dir/'comparisons'/f"{report['annotation_file_sha256']}.json", report)
    (run_dir/'comparisons'/f"{report['annotation_file_sha256']}.md").write_text(render_comparison(report), encoding='utf-8')
    atomic_json(run_dir/'comparison.json', report)
    (run_dir/'comparison.md').write_text(render_comparison(report), encoding='utf-8')
    print(f"模型成功 {report['runtime_status_counts'].get('succeeded', 0)}/{len(items)} 条；参与人工对照 {report['compared']} 条。", flush=True)
    for result in results:
        if result['status'] in {'failed', 'precheck_failed'}:
            print(result['error']['code'] + ' · ' + result['error']['message'], flush=True)
    print('对照报告：' + str(run_dir/'comparison.md'), flush=True)
    if report['compared']:
        label = '复核后的标签一致率' if report.get('annotation_context', {}).get('model_influenced_labels') else '本批对照准确率'
        print(f"{label}：{report['accuracy']:.1%}。样本少，不代表独立验证或生产准确率。", flush=True)
    else:
        print('尚无成功且有人工标签的对照，不能计算准确率。', flush=True)
    return 0 if all(result['status'] == 'succeeded' for result in results) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('评审已中断，已保存的成功项保留；重新运行同一命令可继续。')
        raise SystemExit(130)
    except ReviewPrecheckError as exc:
        print(exc.code + ' · ' + str(exc))
        raise SystemExit(2)
    except (OSError, ValueError, KeyError, TypeError):
        print('CLAIM_EVALUATION_INPUT_ERROR · 无法核对材料、人工标注或运行配置，请检查文件及配置。')
        raise SystemExit(2)
