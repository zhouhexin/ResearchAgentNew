"""查找并下载已收藏论文的开放获取 PDF，随后复用全文解析流程。"""
import hashlib
import logging
import os
import shutil
import sqlite3
from uuid import uuid4
from urllib.parse import urlsplit, urldefrag

import pymupdf
import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from api import uploads
from api.tasks import create_task, update_task
from storage.database import make_db
from storage.db_models import PaperRecord
from tools.open_access_pdf import FulltextError, download_open_pdf, find_open_pdf_urls, pdf_download_settings
from tools.arxiv_pdf import arxiv_id, find_arxiv_pdf_urls
from tools.doi_pdf import find_doi_pdf_urls
from tools.http_diagnostics import request_diagnostic
from tools.venue_search.cvpr import cvf_location, cvf_publication_location, find_cvpr_pdf_urls, find_cvf_pdf_urls
from tools.venue_search.cvpr import cached_get as proceedings_get
from tools.venue_search.proceedings import official_location, find_proceedings_pdf_urls
from tools.venue_search.extended import locate as extended_location, find_extended_pdf_urls

router = APIRouter(prefix='/api/v1/papers', tags=['开放全文'])
logger = logging.getLogger(__name__)


def acquire_fulltext(task_id, paper_id, doi, openalex_id, title='', authors=None, source_url='', settings=None):
    staging = uploads.UPLOAD_ROOT / '.staging' / f'{uuid4().hex}.pdf'
    source_results = []
    attempted_urls, download_attempts = set(), []

    def source_result(provider, status, message, diagnostic=None, *, lookup_status=None, candidate_count=None):
        row = next((row for row in source_results if row['provider'] == provider), None)
        if row is None:
            row = {'provider': provider}
            source_results.append(row)
        row.update(status=status, message=message)
        if lookup_status is not None:
            row['lookup_status'] = lookup_status
        if candidate_count is not None:
            row['candidate_count'] = candidate_count
        row.pop('diagnostic', None)
        if diagnostic:
            row['diagnostic'] = diagnostic
            logger.warning('全文来源请求失败：task=%s provider=%s error_type=%s http_status=%s host=%s causes=%s', task_id, provider, diagnostic.get('error_type'), diagnostic.get('http_status'), diagnostic.get('host'), diagnostic.get('causes'))
        update_task(task_id, source_results=source_results)

    try:
        settings = settings or pdf_download_settings()
        update_task(task_id, status='running', progress=5, stage='正在查找开放获取 PDF 地址')
        downloaded = False
        last_download_error = None
        lookup_errors = []
        providers = [
            ('OpenAlex', lambda: find_open_pdf_urls(doi=doi, openalex_id=openalex_id)),
            ('DOI 原站', lambda: find_doi_pdf_urls(doi, source_url=source_url)),
            ('arXiv', lambda: find_arxiv_pdf_urls(title=title, authors=authors, doi=doi, source_url=source_url)),
        ]
        if arxiv_id(source_url) or arxiv_id(doi):
            providers = [providers[2], providers[1]]
        elif cvf_publication_location(source_url):
            cvf_lookup = find_cvpr_pdf_urls if cvf_location(source_url) else find_cvf_pdf_urls
            providers.insert(0, ('CVF', lambda: cvf_lookup(title, authors, doi, source_url)))
        elif official_location(source_url):
            official=official_location(source_url)
            providers.insert(0,(official['venue_slug'].upper()+' 官方论文集',lambda:find_proceedings_pdf_urls(title,authors,doi,source_url,proceedings_get)))
        elif extended_location(source_url):
            providers.insert(0,('官方论文集',lambda:find_extended_pdf_urls(title,authors,doi,source_url,proceedings_get)))
        for provider, lookup in providers:
            update_task(task_id, progress=5, stage=f'正在通过 {provider} 查找开放 PDF')
            try:
                urls = lookup()
            except FulltextError as exc:
                source_result(provider, 'unavailable', str(exc), lookup_status='unavailable', candidate_count=0)
                if exc.code == 'PAPER_ID_MISMATCH':
                    raise
                lookup_errors.append(exc)
                continue
            except (requests.RequestException, ValueError) as exc:
                if isinstance(exc, requests.RequestException):
                    diagnostic = request_diagnostic(exc)
                    source_result(provider, 'failed', diagnostic['message'], diagnostic, lookup_status='failed')
                else:
                    source_result(provider, 'failed', '论文标识或来源数据无效。', lookup_status='failed')
                lookup_errors.append(exc)
                continue
            source_result(provider, 'found' if urls else 'unavailable',
                          f'已找到 {len(urls)} 个候选下载地址，尚待核对 PDF 内容。' if urls else '未取得可用下载地址。',
                          lookup_status='found' if urls else 'unavailable', candidate_count=len(urls))
            for index, url in enumerate(urls):
                url = urldefrag(url)[0]
                if url in attempted_urls:
                    source_result(provider, 'skipped', '该 PDF 地址已在本次任务尝试过，跳过重复下载。')
                    continue
                update_task(task_id, progress=10, stage=f'正在下载 {provider} PDF（来源 {index + 1}/{len(urls)}）')
                def progress(event):
                    received = event['received_bytes']/1024/1024
                    update_task(task_id, download_progress=dict(event, provider=provider),
                                stage=f'正在下载 {provider} PDF；已接收 {received:.2f} MB')
                try:
                    final_url = download_open_pdf(url, staging, progress=progress, settings=settings, visited_urls=attempted_urls)
                    with pymupdf.open(staging) as pdf:
                        if not pdf.is_pdf or pdf.needs_pass or len(pdf) < 1:
                            raise FulltextError('INVALID_PDF', '下载的 PDF 不可读取或需要密码，请手动下载处理。')
                    downloaded = True
                    source_result(provider, 'succeeded', 'PDF 已成功下载。')
                    break
                except (requests.RequestException, FulltextError, RuntimeError, ValueError) as exc:
                    last_download_error = exc
                    if isinstance(exc, requests.RequestException):
                        diagnostic = getattr(exc, 'download_diagnostic', None) or request_diagnostic(exc)
                        source_result(provider, 'failed', diagnostic['message'], diagnostic)
                    else:
                        diagnostic = getattr(exc, 'diagnostic', None)
                        source_result(provider, 'failed', str(exc) if isinstance(exc, FulltextError) else 'PDF 文件不可读取。', diagnostic)
                    download_attempts.append({'provider':provider, **(diagnostic or {'host':urlsplit(url).hostname,'error_type':type(exc).__name__})})
                    update_task(task_id, download_attempts=download_attempts)
                    if diagnostic and 'received_bytes' in diagnostic:
                        update_task(task_id, download_progress=dict(diagnostic, provider=provider))
                    staging.unlink(missing_ok=True)
            if downloaded:
                break
        if not downloaded:
            if isinstance(last_download_error, FulltextError):
                raise last_download_error
            if last_download_error is not None:
                raise FulltextError('PDF_DOWNLOAD_FAILED', '已尝试可用来源，但 PDF 下载失败，请查看各来源诊断或手动上传。',
                                    getattr(last_download_error, 'download_diagnostic', None))
            for exc in lookup_errors:
                if isinstance(exc, FulltextError) and exc.code not in {'NO_OPEN_PDF', 'NO_ARXIV_MATCH', 'PAPER_ID_REQUIRED', 'NO_DOI_PDF'}:
                    raise exc
            if any(isinstance(exc, requests.RequestException) for exc in lookup_errors):
                raise FulltextError('FULLTEXT_LOOKUP_FAILED', '已尝试 OpenAlex、DOI 原站和 arXiv；部分服务请求失败，未取得可用 PDF，请检查网络后重试。')
            raise FulltextError('NO_OPEN_PDF', 'OpenAlex、DOI 原站和 arXiv 均未找到可确认匹配的开放 PDF，请手动上传。')
        digest = hashlib.sha256(staging.read_bytes()).hexdigest()
        target = uploads.UPLOAD_ROOT / str(paper_id) / f'{digest}.pdf'
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先保留下载文件；即使扫描版无法提取文字，用户仍能在本地找到 PDF。
        if not target.exists():
            shutil.copyfile(staging, target)
        update_task(task_id, progress=15, stage='PDF 已下载到本地，正在解析全文',
                    downloaded=True, download_source_url=final_url, download_provider=provider)
        uploads.process_pdf(task_id, paper_id, staging, digest)
    except Exception as exc:
        code = exc.code if isinstance(exc, FulltextError) else (
            'FULLTEXT_LOOKUP_FAILED' if isinstance(exc, requests.RequestException) else 'FULLTEXT_FETCH_FAILED')
        message = str(exc) if isinstance(exc, FulltextError) else (
            '开放全文服务请求失败，请检查网络、OpenAlex 配置及 arXiv 连通性，或稍后重试。'
            if isinstance(exc, requests.RequestException) else '获取全文失败，请检查网络及本地存储后重试。')
        logger.warning('获取开放 PDF 失败：task=%s, error_type=%s', task_id, type(exc).__name__)
        update_task(task_id, status='failed', stage='获取全文失败', error={
            'code': code, 'message': message, 'retryable': True,
            **({'diagnostic':exc.diagnostic} if isinstance(exc, FulltextError) and exc.diagnostic else {}),
        })
    finally:
        staging.unlink(missing_ok=True)


@router.post('/{paper_id:int}/documents/fetch', status_code=202)
def fetch_fulltext(paper_id: int, background_tasks: BackgroundTasks):
    engine = None
    try:
        settings = pdf_download_settings()
        engine, Session = make_db(os.environ['DATABASE_URL'])
        with Session() as session:
            paper = session.get(PaperRecord, paper_id)
            if paper is None:
                raise HTTPException(404, detail=f'论文不存在：{paper_id}')
            doi, openalex_id = paper.doi, paper.openalex_id
            title, authors, source_url = paper.title, list(paper.authors or []), paper.url
        if not doi and not openalex_id and not arxiv_id(source_url) and not (title.strip() and authors):
            raise HTTPException(422, detail='该论文缺少可用标识或完整标题和作者，暂时无法确认全文来源，请手动上传 PDF。')
        task = create_task(paper_id, deduplicate=True, dedup_key='fetch_fulltext',
                           metadata={'download_timeout':settings['total_timeout']})
        if not task.get('_reused'):
            update_task(task['id'], stage='等待查找开放获取 PDF')
            background_tasks.add_task(acquire_fulltext, task['id'], paper_id, doi, openalex_id, title, authors, source_url, settings)
        return {'task_id': task['id'], 'reused':bool(task.get('_reused'))}
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail='暂时无法查询论文，请检查数据库连接。') from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法创建全文获取任务，请检查本地存储。') from None
    finally:
        if engine is not None:
            engine.dispose()
