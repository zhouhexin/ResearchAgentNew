"""真实 PDF 入库及证据验收：使用现有 py311 环境运行本文件。"""

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from storage.database import Base, make_db
from storage.db_models import PaperRecord, PaperDocumentRecord
from storage.document_repository import PaperDocumentRepository
from tools.document_parser import parse_pdf
from tools.document_verification import verify_pdf, verify_stored_document
from api.pdf_files import local_pdf_path

ROOT = Path(__file__).resolve().parent


def stored_pdf_for_paper(database_url, paper_id):
    """仅从该论文已关联的记录取得本地 PDF，不猜测目录中的文件归属。"""
    engine, Session = make_db(database_url)
    try:
        with Session() as session:
            row = session.scalar(select(PaperDocumentRecord).where(
                PaperDocumentRecord.paper_id == paper_id,
                PaperDocumentRecord.source_type == 'pdf',
            ).order_by(PaperDocumentRecord.id.desc()).limit(1))
            path = local_pdf_path(row) if row is not None else None
            if path is None:
                raise ValueError('该论文没有可读取的本地 PDF，请提供已确认属于该论文的 PDF 路径')
            return path
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path)
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--paper-id", type=int, help="将 PDF 关联到已有论文；不提供路径时重新解析该论文已保存的本地 PDF")
    args = parser.parse_args()
    if args.paper_id is not None and args.paper_id < 1:
        parser.error("--paper-id 必须为正整数")
    load_dotenv(ROOT / '.env')
    if args.paper_id is not None and args.pdf is None:
        pdf_path = stored_pdf_for_paper(os.environ['DATABASE_URL'], args.paper_id)
    else:
        pdf_path = args.pdf or next(iter(sorted((ROOT / "data/pdfs").glob("*.pdf"))), None)
    if pdf_path is None:
        parser.error("请提供 PDF 路径，或将 PDF 放入 data/pdfs/")
    document = parse_pdf(pdf_path)
    report = {"pdf": str(pdf_path.resolve()), **verify_pdf(document, pdf_path)}
    if not args.parse_only:
        load_dotenv(ROOT / ".env")
        url = os.environ["DATABASE_URL"]
        if make_url(url).get_backend_name() != "postgresql":
            raise ValueError("本验收必须使用 PostgreSQL")
        engine, Session = make_db(url)
        try:
            Base.metadata.create_all(engine)
            with Session.begin() as session:
                # 本地材料没有 DOI：按本地来源复用元数据，不捏造论文标识符。
                if args.paper_id is not None:
                    paper = session.get(PaperRecord, args.paper_id)
                    if paper is None:
                        raise ValueError(f"论文不存在：{args.paper_id}")
                else:
                    paper = session.scalar(select(PaperRecord).where(
                        PaperRecord.url == document.source_url
                    ).order_by(PaperRecord.id))
                if paper is None:
                    paper = PaperRecord(title=pdf_path.stem, url=document.source_url)
                    session.add(paper)
                    session.flush()
                paper_id = paper.id
            with PaperDocumentRepository(url) as repo:
                document_id = repo.save(paper_id, document)
                chunk_ids = verify_stored_document(repo, document_id, document)
                repeated_id = repo.save(paper_id, parse_pdf(pdf_path))
                repeated_chunks = verify_stored_document(repo, repeated_id, document)
                with Session() as session:
                    count = session.scalar(select(func.count()).select_from(
                        PaperDocumentRecord
                    ).where(
                        PaperDocumentRecord.paper_id == paper_id,
                        PaperDocumentRecord.source_url == document.source_url,
                    ))
                if repeated_id != document_id or repeated_chunks != chunk_ids or count != 1:
                    raise ValueError("重复导入产生了新文档或新片段")
                report.update(
                    paper_id=paper_id, document_id=document_id,
                    all_database_chunks_verified=True, duplicate_import_verified=True,
                    evidence=repo.get_evidence(document_id, 0),
                )
        finally:
            engine.dispose()
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    name = "document_parse_verification.json" if args.parse_only else "document_verification.json"
    (output / name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except OperationalError:
        raise SystemExit("PostgreSQL 连接失败：请检查网络权限、服务器连通性和 DATABASE_URL。")
