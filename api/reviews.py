"""从固定版本的单篇分析生成比较矩阵和证据驱动的多篇综述。"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from agent.llm_runtime import analysis_llm, model_name, model_endpoint, model_http_backend
from agent.codex_model import CodexExecutionError
from agent.model_diagnostics import model_connection_diagnostic, model_timeout_diagnostic
from api.analyses import model_settings, safe_source
from api.analysis_results import load_result as load_analysis, load_results, with_current_state
from api.review_results import complete_review, load_review, with_review_state
from api.tasks import create_task, update_task
from api import runtime_bindings, provenance, tasks
from agent.runtime_snapshot import RuntimeUnavailable
from review.questions import suggest_questions, QUESTION_PROMPT, QuestionSuggestions
from review.db_guard import verify_sources_in_session
from review.loader import parse_analyses
from review.citations import build_citation_index, snapshot_papers
from review.matrix import render_matrix_csv
from review.synthesis import (build_payload, render_review_md, synthesize_review, ReviewStructureError,
                              ReviewDraft, ReviewValidationError, WRITING_PROFILE, review_output_tokens,
                              prepare_review_request, invoke_review_request)
from storage.database import make_db
from storage.db_models import PaperDocumentRecord, PaperRecord

router = APIRouter(prefix='/api/v1/reviews', tags=['多篇比较与综述'])
ERROR_DIR = Path(__file__).resolve().parents[1] / 'outputs' / 'api_review_errors'
logger = logging.getLogger(__name__)


class ReviewSelection(BaseModel):
    paper_ids: list[int] = Field(min_length=2, max_length=5)
    analysis_ids: list[str] | None = Field(default=None, min_length=2, max_length=5)

    @model_validator(mode='after')
    def valid_selection(self):
        if any(key < 1 for key in self.paper_ids) or len(set(self.paper_ids)) != len(self.paper_ids):
            raise ValueError('请选择 2–5 篇不同的有效论文。')
        if self.analysis_ids is not None and (len(self.analysis_ids) != len(self.paper_ids) or len(set(self.analysis_ids)) != len(self.analysis_ids)):
            raise ValueError('分析版本必须与所选论文一一对应，且不能重复。')
        return self


class ReviewRequest(ReviewSelection):
    topic: str = Field(min_length=1, max_length=1000)
    source_question_task_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def valid_topic(self):
        self.topic = self.topic.strip()
        if not self.topic:
            raise ValueError('请输入与所选论文相关的综述主题。')
        return self


class ReviewInputsChanged(ValueError):
    pass


def verify_inputs(session, snapshots, papers):
    if not all(result['is_current'] for result in with_current_state(snapshots, session)):
        raise ReviewInputsChanged('所选分析的全文或片段已更新，请重新分析。')
    try:
        verify_sources_in_session(session, papers)
    except ValueError as exc:
        raise ReviewInputsChanged(str(exc)) from None


def prepare_selection(body):
    engine = None
    try:
        engine, Session = make_db(os.environ['DATABASE_URL'])
        with Session() as session:
            found = set(session.scalars(select(PaperRecord.id).where(PaperRecord.id.in_(body.paper_ids))))
            missing = set(body.paper_ids) - found
            if missing:
                raise HTTPException(404, detail='论文不存在：' + ', '.join(f'P{key}' for key in sorted(missing)))
            snapshots = []
            for index, paper_id in enumerate(body.paper_ids):
                if body.analysis_ids is not None:
                    snapshot = load_analysis(body.analysis_ids[index])
                    if snapshot is None or snapshot['paper']['id'] != paper_id:
                        raise HTTPException(422, detail=f'P{paper_id} 的指定分析版本不存在或不属于该论文。')
                    candidates = with_current_state([snapshot], session)
                else:
                    candidates = with_current_state(load_results(paper_id), session)
                snapshot = next((value for value in candidates if value['is_current']), None)
                if snapshot is None:
                    raise HTTPException(422, detail=f'P{paper_id} 没有当前有效的分析，请先完成单篇分析。')
                snapshots.append(deepcopy(snapshot))
            # API 报告隐藏本地文件路径；服务端核验时恢复数据库中的真实来源。
            for snapshot in snapshots:
                document = session.get(PaperDocumentRecord, snapshot['document']['id'])
                if document is None:
                    raise HTTPException(422, detail='所选全文已删除，请重新选择论文。')
                snapshot['document']['source_url'] = document.source_url
                for evidence in snapshot['evidence_index'].values():
                    evidence['source_url'] = document.source_url
            papers = parse_analyses(snapshots)
            verify_inputs(session, snapshots, papers)
            _, allowed = build_payload(papers)
            for paper in papers:
                if not any(ref.startswith(f'P{paper.paper_id}:') for ref in allowed):
                    raise HTTPException(422, detail=f'P{paper.paper_id} 没有可用于综述的带证据结论，请重新分析或调整选择。')
        return snapshots, papers
    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取分析快照或原文，请检查数据库和本地存储。') from None
    except ValueError:
        raise HTTPException(422, detail='分析与当前论文或原文证据不一致，请重新分析后再生成综述。') from None
    finally:
        if engine is not None:
            engine.dispose()


def matrix_payload(snapshots, papers):
    return [{'paper_id': paper.paper_id, 'title': paper.title, 'doi': paper.doi,
             'analysis_id': snapshot['id'], 'document_id': paper.document_id,
             'content_sha256': paper.document_hash, 'analysis': paper.analysis.model_dump(mode='json')}
            for snapshot, paper in zip(snapshots, papers)]


@router.post('/compare')
def compare_papers(body: ReviewSelection):
    snapshots, papers = prepare_selection(body)
    return {'paper_ids': body.paper_ids, 'analysis_ids': [p['id'] for p in snapshots],
            'matrix': matrix_payload(snapshots, papers)}


@runtime_bindings.task_worker
def run_question_suggestions(task_id, snapshots, papers, settings):
    engine = None
    try:
        update_task(task_id, status='running', progress=10, stage='正在核对所选论文的有效分析')
        engine, Session = make_db(os.environ['DATABASE_URL'])
        with Session() as session:
            verify_inputs(session, snapshots, papers)
        update_task(task_id, progress=30, stage='正在根据所选论文推荐研究问题')
        binding = runtime_bindings.for_task(task_id, 'questions')
        if binding:
            settings = binding.settings
        with (binding.llm() if binding else analysis_llm(read_timeout=settings['timeout'], retries=settings['retries'], trust_env=True)) as llm:
            suggestions = suggest_questions(llm, papers, method=settings['method'])
        with Session.begin() as session:
            session.scalars(select(PaperDocumentRecord).where(PaperDocumentRecord.id.in_([p.document_id for p in papers])).order_by(PaperDocumentRecord.id).with_for_update()).all()
            verify_inputs(session, snapshots, papers)
            update_task(task_id, status='succeeded', progress=100,
                        stage='研究问题推荐完成' if suggestions.questions else '所选材料暂不足以推荐共同研究问题',
                        question_suggestions=suggestions.model_dump(mode='json')['questions'])
    except Exception as exc:
        diagnostic, retryable = None, True
        if isinstance(exc, RuntimeUnavailable):
            code, message, retryable = exc.code, str(exc), False
        elif isinstance(exc, CodexExecutionError):
            code, message, diagnostic, retryable = exc.code, str(exc), exc.diagnostic, exc.retryable
        elif isinstance(exc, APITimeoutError):
            diagnostic = model_timeout_diagnostic(exc, settings['timeout'])
            code, message = 'MODEL_TIMEOUT', diagnostic['message']
        elif isinstance(exc, APIConnectionError):
            diagnostic = model_connection_diagnostic(exc)
            code, message = 'MODEL_CONNECTION_ERROR', diagnostic['message']
        elif isinstance(exc, APIStatusError):
            code, message = 'MODEL_HTTP_ERROR', f'模型服务返回 HTTP {exc.status_code}，请检查权限、额度及模型配置。'
        elif isinstance(exc, ReviewInputsChanged):
            code, message = 'REVIEW_INPUTS_CHANGED', '推荐期间原文或证据已更新，请重新查看比较矩阵后推荐。'
        elif isinstance(exc, ReviewValidationError):
            code, message = 'QUESTION_VALIDATION_FAILED', '推荐问题未通过跨论文引用核验，请重新推荐或自行填写研究问题。'
        elif isinstance(exc, ReviewStructureError):
            code, message = 'QUESTION_STRUCTURE_FAILED', '模型未返回有效的研究问题列表，请重新推荐或自行填写。'
        elif isinstance(exc, SQLAlchemyError):
            code, message = 'DATABASE_ERROR', '无法核对推荐问题的原文证据，请检查数据库后重试。'
        elif isinstance(exc, (sqlite3.Error, OSError)):
            code, message = 'QUESTION_STORAGE_ERROR', '无法保存推荐问题，请检查服务端存储。'
        else:
            code, message = 'QUESTION_SUGGESTION_FAILED', '研究问题推荐失败，请重新推荐或自行填写。'
        logger.warning('研究问题推荐失败：task=%s code=%s error_type=%s diagnostic=%s', task_id, code, type(exc).__name__, diagnostic)
        try:
            update_task(task_id, status='failed', stage='研究问题推荐失败', error={
                'code': code, 'message': message, 'retryable': retryable,
                **({'diagnostic': diagnostic} if diagnostic else {})})
        except (sqlite3.Error, OSError):
            logger.error('无法保存推荐问题任务状态：task=%s', task_id)
    finally:
        if engine is not None:
            engine.dispose()


@router.post('/questions', status_code=202)
def start_question_suggestions(body: ReviewSelection, background_tasks: BackgroundTasks):
    snapshots, papers = prepare_selection(body)
    try:
        bundle = runtime_bindings.bundle_for()
        binding = runtime_bindings.restore(bundle, 'questions') if bundle else None
        settings = binding.settings if binding else model_settings()
        versions = sorted((paper.paper_id, snapshot['id']) for paper, snapshot in zip(papers, snapshots))
        key = hashlib.sha256(json.dumps([versions, binding.metadata if binding else [model_name(), model_endpoint()], settings['method'],
                                        QUESTION_PROMPT, QuestionSuggestions.model_json_schema()], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        task = create_task(task_type='review_questions', stage='等待推荐研究问题', deduplicate=True, dedup_key=key,
                           metadata={'paper_ids': [p.paper_id for p in papers], 'analysis_ids': [s['id'] for s in snapshots],
                                     'model_name': binding.runtime.model if binding else model_name(), 'http_backend': binding.runtime.backend if binding else model_http_backend(), 'read_timeout': settings['timeout'],
                                     **runtime_bindings.metadata(bundle, 'questions')})
        if not task.get('_reused'):
            background_tasks.add_task(run_question_suggestions, task['id'], snapshots, papers, settings)
        return {'task_id': task['id'], 'reused': bool(task.get('_reused'))}
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法创建研究问题推荐任务。') from None


def verify_linked_review_context(context):
    if not context or not context.get('review_selection_id'):
        return
    from api import coverage
    try:
        run=coverage.source_run(context['plan_id'],context['execution_id'])
        if run.get('review_selection_id')!=context['review_selection_id']:
            raise RuntimeUnavailable('报告关联的评审选择已变化。')
    except HTTPException as exc:
        if exc.status_code>=500:raise
        raise RuntimeUnavailable('报告关联的评审或执行材料已变化，请重新确认版本。') from None


@runtime_bindings.task_worker
def run_review(task_id, snapshots, papers, topic, settings, research_context=None):
    engine = None
    try:
        update_task(task_id, status='running', progress=10, stage='正在核对固定的分析版本和原文证据')
        engine, Session = make_db(os.environ['DATABASE_URL'])
        with Session() as session:
            verify_inputs(session, snapshots, papers)
        verify_linked_review_context(research_context)
        csv = render_matrix_csv(papers)
        update_task(task_id, progress=30, stage='比较矩阵已生成，正在撰写带解释和比较依据的详细综述；可能需要数分钟')
        binding = runtime_bindings.for_task(task_id, 'review')
        if binding:
            settings = binding.settings
        with (binding.llm() if binding else analysis_llm(read_timeout=settings['timeout'], retries=settings['retries'], trust_env=True,
                          max_output_tokens=settings['output_tokens'])) as llm:
            focus=topic
            if research_context:
                focus+='\n研究目标：'+research_context['goal']
                focus+='\n比较维度：'+ '、'.join(research_context['comparison_dimensions'])
                focus+='\n阅读范围：'+json.dumps(research_context['reading_coverage'],ensure_ascii=False)
                focus+='\n按以上研究问题和维度进行有证据的比较；无法比较或未阅读的部分须明确说明，不得补造数据或声称完整领域查新。'
            request = prepare_review_request(papers, method=settings['method'], topic=focus)
            # 调用前持久化，失败草稿也有可回放的实际输入。
            update_task(task_id, synthesis_request=request)
            draft, _ = invoke_review_request(llm, request)
        update_task(task_id, progress=85, stage='正在核验跨论文引用和原文版本')
        public_papers = [replace(paper, source_url=safe_source(paper.source_url), evidence_index={key: dict(value, source_url=safe_source(value['source_url'])) for key, value in paper.evidence_index.items()}) for paper in papers]
        payload = {'paper_ids': [p.paper_id for p in papers], 'analysis_ids': [s['id'] for s in snapshots],
                   'topic': topic, 'model_name': binding.runtime.model if binding else model_name(), 'writing_profile': WRITING_PROFILE, 'draft': draft.model_dump(mode='json'),
                   'matrix': matrix_payload(snapshots, papers),
                   'evidence_index': {f'P{p.paper_id}:{key}': value for p in public_papers for key, value in p.evidence_index.items()},
                   'citation_index': build_citation_index(draft, public_papers)}
        payload['synthesis_request'] = request
        if research_context:
            payload['research_context']=research_context
        payload.update(provenance.fields(tasks.get_task(task_id)))
        payload['source_task_id'] = task_id
        source_task = tasks.get_task(task_id)
        if source_task.get('source_question_task_id'):
            payload['source_question_task_id'] = source_task['source_question_task_id']
            payload['question_edited'] = source_task.get('question_edited')
        if binding:
            binding.check()
            payload.update(binding.metadata)
        verify_linked_review_context(research_context)
        markdown = render_saved_review(payload)
        with Session.begin() as session:
            # 按固定顺序锁定全文，核验通过后再原子发布报告与下载内容。
            session.scalars(select(PaperDocumentRecord).where(PaperDocumentRecord.id.in_([p.document_id for p in papers])).order_by(PaperDocumentRecord.id).with_for_update()).all()
            verify_inputs(session, snapshots, papers)
            complete_review(task_id, payload, markdown, csv)
    except Exception as exc:
        diagnostic = None
        if isinstance(exc, RuntimeUnavailable):
            code, message = exc.code, str(exc)
        elif isinstance(exc, CodexExecutionError):
            code, message, diagnostic = exc.code, str(exc), exc.diagnostic
        elif isinstance(exc, APITimeoutError):
            diagnostic = model_timeout_diagnostic(exc, settings['timeout'])
            code, message = 'MODEL_TIMEOUT', diagnostic['message'] + ' 未发布本次综述报告。'
        elif isinstance(exc, APIConnectionError):
            diagnostic = model_connection_diagnostic(exc)
            code, message = 'MODEL_CONNECTION_ERROR', diagnostic['message']
        elif isinstance(exc, APIStatusError):
            code, message = 'MODEL_HTTP_ERROR', f'模型服务返回 HTTP {exc.status_code}，请检查权限、额度及模型配置。'
        elif isinstance(exc, ReviewValidationError):
            code, message = 'REVIEW_VALIDATION_FAILED', '综述未通过引用核验，未发布报告。每条结论必须有合法引用，整份报告必须实际引用至少两篇论文。'
            diagnostic = {'reason':exc.diagnostics.get('validation_reason', 'citation_contract_failed'),
                          'phase':'review_validation'}
            if diagnostic['reason'] == 'comparison_claim_requires_multiple_papers':
                message = '跨论文比较的主题句须引用至少两篇论文；仅在解释段落引用不满足当前规则。未发布报告，请核对比较结构。'
                diagnostic.update(section='comparisons', point_index=exc.diagnostics['point_index'])
        elif isinstance(exc, ReviewStructureError):
            code, message = 'REVIEW_STRUCTURE_FAILED', '模型未返回完整有效的结构化综述，未发布报告。'
        elif isinstance(exc, ReviewInputsChanged):
            code, message = 'REVIEW_INPUTS_CHANGED', '生成期间原文或证据已更新，未发布本次综述，请重新分析并生成。'
        elif isinstance(exc, SQLAlchemyError):
            code, message = 'DATABASE_ERROR', '无法核对综述的原文证据，请检查数据库后重试。'
        elif isinstance(exc, (sqlite3.Error, OSError)):
            code, message = 'REVIEW_STORAGE_ERROR', '无法保存综述，请检查服务端存储。'
        else:
            code, message = 'REVIEW_FAILED', '综述任务失败，未发布报告，请检查服务日志后重试。'
        if diagnostic is not None or isinstance(exc, (ReviewValidationError, ReviewStructureError)):
            try:
                ERROR_DIR.mkdir(parents=True, exist_ok=True)
                (ERROR_DIR / f'{task_id}.json').write_text(json.dumps(exc.diagnostics if isinstance(exc, (ReviewValidationError, ReviewStructureError)) else diagnostic, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError:
                logger.warning('无法保存综述诊断：task=%s', task_id)
        logger.warning('多篇综述失败：task=%s error_type=%s code=%s diagnostic=%s', task_id, type(exc).__name__, code, diagnostic)
        try:
            update_task(task_id, status='failed', stage='综述生成失败', error={'code': code, 'message': message, 'retryable': False if isinstance(exc, RuntimeUnavailable) else exc.retryable if isinstance(exc, CodexExecutionError) else True, **({'diagnostic': diagnostic} if diagnostic else {})})
        except (sqlite3.Error, OSError):
            logger.error('无法保存综述失败状态：task=%s', task_id)
    finally:
        if engine is not None:
            engine.dispose()


@router.post('', status_code=202)
def start_review(body: ReviewRequest, background_tasks: BackgroundTasks):
    if body.source_question_task_id:
        source = tasks.get_task(body.source_question_task_id)
        if source['type'] != 'review_questions' or source['status'] != 'succeeded':
            raise HTTPException(422, detail='研究问题来源必须是已完成的推荐任务。')
        snapshots, _ = prepare_selection(body)
        if set(source.get('analysis_ids', [])) != {s['id'] for s in snapshots}:
            raise HTTPException(409, detail='研究问题来源与本次分析版本不一致，请重新推荐或自行填写。')
        with tasks.task_scope(source['id']):
            return create_review(body, background_tasks)
    return create_review(body,background_tasks)


def create_review(body,background_tasks,research_context=None):
    snapshots, papers = prepare_selection(body)
    try:
        bundle = runtime_bindings.bundle_for()
        binding = runtime_bindings.restore(bundle, 'review') if bundle else None
        settings = dict(binding.settings) if binding else model_settings()
        if not binding:
            settings['output_tokens'] = review_output_tokens()
        versions = sorted((p.paper_id, s['id']) for p, s in zip(papers, snapshots))
        key = hashlib.sha256(json.dumps([versions, body.topic, body.source_question_task_id,
            binding.metadata if binding else [model_name(), model_endpoint()], WRITING_PROFILE, settings['output_tokens']], ensure_ascii=False).encode()).hexdigest()
        if research_context:key=hashlib.sha256(json.dumps([key,research_context],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        task = create_task(task_type='review_generation', stage='等待生成比较矩阵与综述', deduplicate=True, dedup_key=key,
                           metadata={'paper_ids': body.paper_ids, 'analysis_ids': [s['id'] for s in snapshots], 'topic': body.topic,
                                     'research_questions': [body.topic],
                                     **({'source_question_task_id': body.source_question_task_id,
                                         'question_edited': body.topic not in [q['question'] for q in
                                            (tasks.CURRENT_TASK.get() or {}).get('question_suggestions', [])]}
                                        if body.source_question_task_id else {}),
                                     'model_name': binding.runtime.model if binding else model_name(), 'http_backend': binding.runtime.backend if binding else model_http_backend(), 'read_timeout': settings['timeout'],
                                     **runtime_bindings.metadata(bundle, 'review')})
        if not task.get('_reused'):
            background_tasks.add_task(run_review, task['id'], snapshots, papers, body.topic, settings, research_context)
        return {'task_id': task['id'], 'reused': bool(task.get('_reused'))}
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法创建综述任务，请检查本地存储。') from None


def render_saved_review(payload):
    """重新排版冻结报告，旧报告也无需重新请求模型或更新数据库。"""
    papers = snapshot_papers(payload)
    markdown = render_review_md(ReviewDraft.model_validate(payload['draft']), papers)
    markdown += '\n## 使用的分析版本\n\n' + '\n'.join(
        f'- P{p.paper_id}：{key}；全文 SHA256：{p.document_hash}'
        for p, key in zip(papers, payload.get('analysis_ids', []))) + '\n'
    context = payload.get('research_context')
    if context:
        markdown += '\n## 计划与阅读范围\n\n' + (
            f"- 计划：{context['plan_id']}；版本：{context['plan_revision']}\n"
            f"- 本轮执行：{context['execution_id']}\n- 确认的研究问题：{payload['topic']}\n"
            '- 比较维度：' + '、'.join(context['comparison_dimensions']) + '\n')
        for coverage in context['reading_coverage']:
            markdown += (f"- P{coverage['paper_id']}：{coverage['scope']}，阅读 "
                         f"{coverage['selected_chunks']}/{coverage['total_chunks']} 个片段。"
                         + coverage.get('note', '') + '\n')
        if context.get('auxiliary_reviews'):
            markdown += '\n### 辅助评审记录\n\n模型判断供人工核对；本报告未据标签自动删除或改写分析结论。\n\n'
            for review in context['auxiliary_reviews']:
                markdown += f"- P{review['paper_id']}：{review['review_id']}；分析版本：{review['analysis_id']}\n"
    if payload.get('interaction_id'):
        markdown += '\n## 执行来源\n\n'
        for key, label in (('interaction_id', '交互'), ('source_task_id', '来源任务'),
                           ('agent_version_id', '执行版本'), ('runtime_snapshot_id', '运行快照'),
                           ('source_question_task_id', '研究问题推荐任务')):
            if payload.get(key):
                markdown += f'- {label}：{payload[key]}\n'
        markdown += '- 确认的研究问题：' + payload['topic'] + '\n'
    return markdown


def read_review(review_id):
    engine = None
    try:
        stored = load_review(review_id)
        if stored is None:
            raise HTTPException(404, detail='综述结果不存在。')
        payload, markdown, csv = stored
        engine, Session = make_db(os.environ['DATABASE_URL'])
        with Session() as session:
            payload = with_review_state(payload, session)
        if payload.get('research_context',{}).get('review_selection_id'):
            from api.analysis_runtime import RuntimeStorageUnavailable
            try:
                verify_linked_review_context(payload['research_context'])
                payload['review_links_current']=True
            except RuntimeStorageUnavailable:
                raise
            except (RuntimeUnavailable, ValueError):
                payload['review_links_current']=False
                payload['is_current']=False
        # 只从当初冻结的分析和原文生成展示信息，历史报告不借用较新版本。
        payload['citation_index'] = build_citation_index(
            ReviewDraft.model_validate(payload['draft']), snapshot_papers(payload))
        return payload, markdown, csv
    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取综述或核对原文版本。') from None
    finally:
        if engine is not None:
            engine.dispose()


@router.get('/{review_id}')
def get_review(review_id: str):
    return read_review(review_id)[0]


@router.get('/{review_id}/files/{format}')
def download_review(review_id: str, format: str):
    if format not in {'json', 'md', 'csv'}:
        raise HTTPException(422, detail='下载格式仅支持 json、md 或 csv。')
    payload, markdown, csv = read_review(review_id)
    if format == 'md':
        markdown = render_saved_review(payload)
    content, media_type, filename = {
        'json': (json.dumps(payload, ensure_ascii=False, indent=2), 'application/json', 'literature_review.json'),
        'md': (markdown, 'text/markdown', 'literature_review.md'),
        'csv': ('\ufeff' + csv, 'text/csv', 'comparison_matrix.csv'),
    }[format]
    return Response(content, media_type=media_type, headers={'Content-Disposition': f'attachment; filename="{filename}"', 'X-Content-Type-Options': 'nosniff'})
