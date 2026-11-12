
import json
import os
import argparse
import math
from time import monotonic
from uuid import uuid4

from pathlib import Path
from dotenv import load_dotenv
from openai import APITimeoutError, APIConnectionError, APIStatusError

from agent.paper_context import (
    load_paper_context,
    assert_document_unchanged,
    verify_paper_context,
    DocumentChangedError,
)
from agent.llm_runtime import analysis_llm, model_request_options, model_http_backend, model_name, model_endpoint, model_provider, validate_model_configuration
from agent.codex_model import CodexExecutionError
from agent.model_diagnostics import model_connection_diagnostic, model_timeout_diagnostic
from agent.paper_analyzer import StructuredAnalysisError
from agent.batch_analysis import analyze_batches, split_context, plan_fingerprint, BatchAnalysisFailed
from storage.analysis_batches import AnalysisBatchStore
from storage.database import make_db
from storage.pdf_reparse import ensure_pdf_reading_order
from agent.analysis_validator import export_analysis
from agent.analysis_validator import EvidenceValidationError
from agent.model_check import check_structured_analysis


ROOT = Path(__file__).resolve().parent


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="分析已有全文，或独立诊断模型连接")
    parser.add_argument("paper_id", nargs="?", type=int)
    parser.add_argument("--check-llm", action="store_true", help="仅发送简短模型请求，不访问数据库")
    parser.add_argument("--structured", action="store_true", help="配合 --check-llm，检查论文分析结构及原文引用")
    parser.add_argument("--timeout", type=float, default=None, help="模型读取超时秒数")
    parser.add_argument("--max-chunks", type=int, default=None, help="抽样片段上限；指定后默认使用抽样模式")
    parser.add_argument("--scope", choices=["full", "sample", "fast"], default=None, help="full 全量；fast 正文关键章节优先；sample 均匀抽样")
    parser.add_argument("--batch-size", type=int, default=2, help="每批片段数，默认 2")
    parser.add_argument("--restart", action="store_true", help="忽略已保存批次，从头重新分析")
    parser.add_argument("--max-chars", type=int, default=int(os.getenv("ANALYSIS_BATCH_MAX_CHARS", "4000")), help="每批原文字符预算")
    parser.add_argument("--retries", "--batch-retries", type=int, default=int(os.getenv("ANALYSIS_BATCH_RETRIES", "1")), help="每批额外尝试次数，默认 1")
    parser.add_argument("--no-proxy", action="store_true", help="不使用环境变量中的代理；同时不读取环境证书配置")
    parser.add_argument('--http-backend', choices=['sdk', 'requests'], default=None, help='选择 HTTP 传输，用相同真实分析对照连接问题')
    args = parser.parse_args()
    if args.structured and not args.check_llm:
        parser.error('--structured 须配合 --check-llm 使用')
    timeout = args.timeout if args.timeout is not None else (
        (120.0 if args.structured else 30.0) if args.check_llm else float(os.getenv("LLM_READ_TIMEOUT", "400"))
    )
    if not math.isfinite(timeout) or timeout <= 0 or not 0 <= args.retries <= 3 or (args.max_chunks is not None and args.max_chunks < 2) or not 1 <= args.batch_size <= 10 or args.max_chars < 1:
        parser.error("超时和字符预算必须为正数，max-chunks 至少为 2，retries 不能为负数")
    if not args.check_llm and (args.paper_id is None or args.paper_id < 1):
        parser.error("请提供有效的 paper_id，或使用 --check-llm")
    trust_env = not args.no_proxy
    proxy_names = [key for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "OPENAI_PROXY"
    ) if os.getenv(key)]
    validate_model_configuration()
    codex = model_provider() == 'codex'
    print(f"{'Codex 单次调用时限' if codex else '读取超时'}：{timeout:g} 秒；每批额外重试：{args.retries} 次", flush=True)
    print("代理：" + ("已禁用环境代理" if not trust_env else
          ("检测到 " + ", ".join(proxy_names) if proxy_names else "未检测到代理环境变量")), flush=True)
    options = model_request_options()
    thinking = options.get('extra_body', {}).get('enable_thinking')
    if not codex:
        print('思考模式：' + ('使用模型服务默认值' if thinking is None else '开启' if thinking else '关闭'), flush=True)
        print(f"最大输出 Token：{options.get('max_tokens', '使用模型服务默认值')}", flush=True)
    print('模型：' + model_name(), flush=True)
    print('连接方式：' + model_http_backend(args.http_backend), flush=True)
    if args.check_llm:
        print('正在检查论文分析结构和原文引用……' if args.structured else "正在发送简短模型请求……", flush=True)
        started = monotonic()
        try:
            with analysis_llm(read_timeout=timeout, retries=0, trust_env=trust_env, http_backend=args.http_backend) as llm:
                if args.structured:
                    check_structured_analysis(llm, os.getenv('STRUCTURED_OUTPUT_METHOD', 'json_schema'))
                else:
                    response = llm.invoke([("human", "请只回复 OK。")])
                    if not response.content:
                        raise ValueError("模型返回了空内容；请检查模型和接口配置")
        except APITimeoutError as exc:
            diagnostic = model_timeout_diagnostic(exc, timeout)
            raise SystemExit(diagnostic['message'] + '\n底层异常链：' + ' → '.join(diagnostic['causes'])) from None
        if args.structured:
            print(f'结构化分析及原文引用核验通过，耗时 {monotonic() - started:.1f} 秒。')
            print('该检查使用两个短示例片段，不代表真实论文的所有批次均可在相同时间内完成。')
            return
        print(f"简短模型请求成功，耗时 {monotonic() - started:.1f} 秒。")
        print("该检查仅验证普通对话连通性，不代表结构化论文分析已通过。")
        return

    paper_id = args.paper_id

    database_url = os.environ["DATABASE_URL"]
    reparsed = ensure_pdf_reading_order(database_url, paper_id, db_factory=make_db)
    if reparsed:
        print(f"已更新 PDF 解析与分片：{reparsed['previous_chunks']} → {reparsed['total_chunks']} 个片段；旧批次不会混入新计划。", flush=True)

    # 1. 加载论文和原文证据
    scope = args.scope or ('sample' if args.max_chunks is not None else 'full')
    if scope=='fast':
        context=load_paper_context(database_url,paper_id,max_chunks=args.max_chunks or 18,fast=True)
        print(context.reading_selection['note'])
    else:
        context = load_paper_context(
            database_url,
            paper_id,
            max_chunks=None if scope == 'full' else (args.max_chunks or int(os.getenv('ANALYSIS_MAX_CHUNKS', '6'))),
        )
    batches = split_context(context, args.batch_size, args.max_chars)

    print(f"正在分析：{context.paper_title}")
    print(
        f"本次阅读片段："
        f"{len(context.sources)}/{context.total_chunks}"
    )
    print(f"分为 {len(batches)} 批，每批最多 {args.batch_size} 个片段；每批核验后保存。", flush=True)
    method = os.getenv("STRUCTURED_OUTPUT_METHOD", "json_schema")
    fingerprint = plan_fingerprint(context, batches, model=model_name(),
        base_url=model_endpoint(), method=method, scope=scope,
        batch_size=args.batch_size, max_chars=args.max_chars)
    store = AnalysisBatchStore(ROOT / "outputs" / "api_tasks.sqlite3")
    run_key = fingerprint + '_' + uuid4().hex if args.restart else (store.latest_run(fingerprint) or fingerprint)
    store.prepare(run_key, fingerprint, batches)
    engine, Session = make_db(database_url)
    def check(batch):
        with Session.begin() as session:
            verify_paper_context(session, batch)
    def save(batch, index, analysis):
        with Session.begin() as session:
            verify_paper_context(session, batch)
            store.save_completed(run_key, index, analysis)
    def llm_factory(**kwargs):
        kwargs['trust_env'] = trust_env
        if args.http_backend is not None:
            kwargs['http_backend'] = args.http_backend
        return analysis_llm(**kwargs)
    def progress(**event):
        text = {'cached': '复用已保存结果', 'running': '正在请求模型', 'completed': '核验通过并保存', 'retrying': '只重试本批'}[event['state']]
        if event.get('retry_delay_seconds'):
            text = f"{event['retry_delay_seconds']} 秒后重试本批"
        print(f"第 {event['index']}/{event['total']} 批：{text}；已完成 {event['completed']} 批", flush=True)
    try:
        check(context)
        analysis, summary = analyze_batches(context, batches, run_key=run_key, store=store,
            llm_factory=llm_factory, read_timeout=timeout, method=method,
            batch_retries=args.retries, check_context=check, save_batch=save, progress=progress)
        assert_document_unchanged(database_url, context)
        check(context)
    except BatchAnalysisFailed as exc:
        print(f"第 {exc.index} 批未完成。已经核验的批次保留，使用不带 --restart 的同一分析命令可继续；--restart 会从头开始。", flush=True)
        raise exc.cause
    finally:
        engine.dispose()

    # 5. 导出经过证据检查的结果
    result = export_analysis(
        analysis,
        context,
    )

    result['batch_summary'] = dict(summary, batch_size=args.batch_size, scope=scope)
    result['selected_evidence_ids'] = list(context.sources)
    result['model_name'] = model_name()
    output_dir = ROOT / "outputs"
    output_dir.mkdir(exist_ok=True)

    output_path = (
        output_dir
        / f"paper_analysis_{paper_id}_{context.document_id}.json"
    )

    output_path.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"分析完成：{output_path}")


if __name__ == "__main__":
    try:
        main()
    except CodexExecutionError as exc:
        diagnostic_path = ROOT / 'outputs' / 'codex_diagnostic.json'
        diagnostic_path.parent.mkdir(exist_ok=True)
        diagnostic_path.write_text(json.dumps(exc.diagnostic, ensure_ascii=False, indent=2), encoding='utf-8')
        raise SystemExit(f'{exc.code} · {exc}\n安全诊断：' + json.dumps(exc.diagnostic, ensure_ascii=False)
                         + f'\n诊断文件：{diagnostic_path}') from None
    except DocumentChangedError:
        raise SystemExit("原文或片段已改变，未合并成报告。请使用当前原文重新开始，旧版本进度不会混用。") from None
    except APITimeoutError:
        raise SystemExit(
            "当前批次模型请求超时，已完成批次保留，未导出不完整报告。可使用相同命令继续。也可先运行 python run_paper_analysis.py --check-llm；"
            "若小请求也超时，请检查模型服务和代理，可加 --no-proxy 对比。"
            "若小请求正常，可使用 --max-chunks 4 --timeout 300 重试分析。"
        ) from None
    except EvidenceValidationError as exc:
        diagnostic_path = ROOT / "outputs" / "paper_analysis_evidence_error.json"
        diagnostic_path.parent.mkdir(exist_ok=True)
        diagnostic_path.write_text(
            json.dumps(exc.diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise SystemExit(
            f"分析未通过原文证据核验，未导出本次报告：{exc}\n"
            f"候选引文与原文对照已保存：{diagnostic_path}"
        ) from None
    except StructuredAnalysisError as exc:
        diagnostic_path = ROOT / "outputs" / "paper_analysis_response_error.json"
        diagnostic_path.parent.mkdir(exist_ok=True)
        diagnostic_path.write_text(
            json.dumps(exc.diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise SystemExit(
            f"结构化分析失败，未导出本次报告：{exc}\n"
            f"接口响应诊断已保存：{diagnostic_path}"
        ) from None
    except APIConnectionError as exc:
        diagnostic = model_connection_diagnostic(exc)
        raise SystemExit(
            diagnostic['message'] + '\n底层异常链：' + ' → '.join(diagnostic['causes']) +
            '\n已完成批次保留。可运行 python run_paper_analysis.py --check-llm --no-proxy 对比。'
        ) from None
    except APIStatusError as exc:
        raise SystemExit(
            f"模型接口返回 HTTP {exc.status_code}，未导出本次分析。"
            "请检查接口权限、额度以及模型对结构化输出模式的支持。"
        ) from None
