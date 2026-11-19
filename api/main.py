from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

import os

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from storage.database import make_db

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from api.papers import router as papers_router
from api.documents import router as documents_router
from api.uploads import router as uploads_router
from api.fulltext import router as fulltext_router
from api.analyses import router as analyses_router
from api.reviews import router as reviews_router
from api.tasks import router as tasks_router, recover_interrupted_tasks
from api.model_checks import router as model_checks_router
from api.research_plans import router as research_plans_router
from api.research_search import router as research_search_router
from api.research_execution import router as research_execution_router
from api.plan_reports import router as plan_reports_router
from api.venues import router as venues_router
from api.abstract_translations import router as abstract_translations_router
from api.claim_reviews import router as claim_reviews_router
from api.coverage import router as coverage_router
from api.rereads import router as rereads_router
from api.supplement_search import router as supplement_search_router
from api.supplement_readings import router as supplement_readings_router
from api.execution_review_links import router as execution_review_links_router
from api.analyses import model_settings
from agent.llm_runtime import model_http_backend, model_name


@asynccontextmanager
async def lifespan(app):
    recover_interrupted_tasks()
    yield

app = FastAPI(
    title="ResearchPilot API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(papers_router)
app.include_router(documents_router)
app.include_router(uploads_router)
app.include_router(fulltext_router)
app.include_router(tasks_router)
app.include_router(model_checks_router)
app.include_router(research_plans_router)
app.include_router(research_search_router)
app.include_router(research_execution_router)
app.include_router(plan_reports_router)
app.include_router(venues_router)
app.include_router(abstract_translations_router)
app.include_router(analyses_router)
app.include_router(claim_reviews_router)
app.include_router(reviews_router)
app.include_router(coverage_router)
app.include_router(rereads_router)
app.include_router(supplement_search_router)
app.include_router(supplement_readings_router)
app.include_router(execution_review_links_router)

@app.get("/api/v1/ping")
def ping():
    return {
        "message": "ResearchPilot API 已启动",
    }


@app.get("/api/v1/system/health")
def system_health():
    database_ok = False
    engine = None

    database_url = os.getenv("DATABASE_URL")

    if database_url:
        try:
            engine, _ = make_db(database_url)

            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))

            database_ok = True

        except (SQLAlchemyError, ValueError, ImportError):
            database_ok = False

        finally:
            if engine is not None:
                engine.dispose()

    model_error = None
    backend = None
    try:
        model_settings()
        backend = model_http_backend()
    except ValueError as exc:
        model_error = str(exc)
    return {
        "api": True,
        "database": database_ok,
        "model_configured": model_error is None,
        "model_name": model_name() if model_error is None else None,
        "model_http_backend": backend,
        "model_configuration_error": model_error,
    }
