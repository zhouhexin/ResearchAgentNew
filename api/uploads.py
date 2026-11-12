"""将用户确认归属的 PDF 交给现有解析、入库流程。"""
import hashlib
import logging
import os
import sqlite3
from pathlib import Path
from threading import Lock
from uuid import uuid4

import pymupdf
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from sqlalchemy.exc import SQLAlchemyError

from api.tasks import create_task, update_task
from storage.database import make_db
from storage.db_models import PaperRecord
from storage.document_repository import PaperDocumentRepository
from tools.document_parser import parse_pdf

router = APIRouter(prefix='/api/v1/papers', tags=['全文上传'])
UPLOAD_ROOT = Path(__file__).resolve().parents[1] / 'data' / 'pdfs' / 'uploads'
MAX_PDF_BYTES = 20 * 1024 * 1024
IMPORT_LOCK = Lock()
logger = logging.getLogger(__name__)


def database_diagnostic(exc):
    # 不记录 SQL、绑定参数、连接地址或驱动异常原文（其中可能含正文与凭据）。
    original=getattr(exc,'orig',None)
    state=getattr(original,'sqlstate',None) or getattr(original,'pgcode',None)
    state=state if isinstance(state,str) and len(state)==5 and state.isascii() and state.isalnum() else None
    return {'reason':'database_write','error_type':type(exc).__name__,
            'driver_error_type':type(original).__name__,'sqlstate':state}


def paper_exists(paper_id):
    engine = None
    try:
        engine, Session = make_db(os.environ['DATABASE_URL'])
        with Session() as session:
            if session.get(PaperRecord, paper_id) is None:
                raise HTTPException(404, detail=f'论文不存在：{paper_id}')
    except SQLAlchemyError:
        raise HTTPException(503, detail='暂时无法查询论文，请检查数据库连接。') from None
    finally:
        if engine is not None:
            engine.dispose()


def process_pdf(task_id: str, paper_id: int, staged_path: Path, digest: str):
    try:
        # 同一进程顺序入库，避免同时上传相同 PDF 时产生唯一约束竞争。
        with IMPORT_LOCK:
            update_task(task_id, status='running', progress=15, stage='正在提取 PDF 正文和页码')
            document = parse_pdf(staged_path)
            update_task(task_id, progress=60, stage='正在校验原文位置并保存全文')
            target = UPLOAD_ROOT / str(paper_id) / f'{digest}.pdf'
            target.parent.mkdir(parents=True, exist_ok=True)
            staged_path.replace(target)
            document.source_url = target.resolve().as_uri()
            with PaperDocumentRepository(os.environ['DATABASE_URL']) as repo:
                document_id = repo.save(paper_id, document)
            update_task(task_id, status='succeeded', progress=100, result_id=str(document_id),
                        stage=f'全文已保存：{document.page_count} 页，{len(document.chunks)} 个片段')
    except Exception as exc:
        diagnostic=None
        if isinstance(exc, SQLAlchemyError):
            code, message = 'DATABASE_ERROR', '数据库保存失败，请检查连接和数据表后重新上传。'
            diagnostic=database_diagnostic(exc)
        elif isinstance(exc, ValueError):
            code, message = 'PDF_PARSE_ERROR', '无法提取有效正文。扫描版 PDF 可能需要先做 OCR；加密或损坏的 PDF 请先修复。'
        else:
            code, message = 'PDF_IMPORT_ERROR', 'PDF 处理失败，请检查文件是否有效及服务端目录是否可写。'
        logger.warning('PDF 导入任务失败：task=%s, error_type=%s diagnostic=%s', task_id, type(exc).__name__,diagnostic)
        try:
            update_task(task_id, status='failed', stage='全文导入失败', error={
                'code': code, 'message': message, 'retryable': True,
                **({'diagnostic':diagnostic} if diagnostic else {}),
            })
        except (sqlite3.Error, OSError):
            logger.error('无法保存 PDF 任务失败状态：task=%s', task_id)
    finally:
        staged_path.unlink(missing_ok=True)


@router.post('/{paper_id:int}/documents', status_code=202)
def upload_pdf(
    paper_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    confirmed: bool = Form(False),
):
    staged_path = None
    try:
        if not confirmed:
            raise HTTPException(422, detail='请先确认 PDF 对应当前论文。')
        paper_exists(paper_id)
        if not file.filename or not file.filename.lower().endswith('.pdf'):
            raise HTTPException(422, detail='请选择 PDF 文件。')
        staging = UPLOAD_ROOT / '.staging'
        staging.mkdir(parents=True, exist_ok=True)
        staged_path = staging / f'{uuid4().hex}.pdf'
        digest = hashlib.sha256()
        size = 0
        with staged_path.open('xb') as destination:
            while block := file.file.read(1024 * 1024):
                if size == 0 and not block.startswith(b'%PDF-'):
                    raise HTTPException(422, detail='文件内容不是有效的 PDF。')
                size += len(block)
                if size > MAX_PDF_BYTES:
                    raise HTTPException(413, detail='PDF 大小不能超过 20 MB。')
                digest.update(block)
                destination.write(block)
        if size == 0:
            raise HTTPException(422, detail='不能上传空文件。')
        try:
            with pymupdf.open(staged_path) as pdf:
                if not pdf.is_pdf or pdf.page_count < 1:
                    raise ValueError('invalid PDF')
                if pdf.needs_pass:
                    raise HTTPException(422, detail='PDF 需要密码，请先解密后上传。')
        except (RuntimeError, ValueError):
            raise HTTPException(422, detail='PDF 已损坏或无法解析，请检查文件。') from None
        task = create_task(paper_id)
        background_tasks.add_task(process_pdf, task['id'], paper_id, staged_path, digest.hexdigest())
        staged_path = None  # 后台任务负责清理临时文件。
        return {'task_id': task['id']}
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法保存上传文件或创建任务，请检查服务端存储。') from None
    finally:
        file.file.close()
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
