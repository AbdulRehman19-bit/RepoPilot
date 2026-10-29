from fastapi import FastAPI

from app.api.executions import router as executions_router
from app.api.issues import router as issues_router
from app.api.repositories import router as repositories_router
from app.config import settings
from app.github.router import router as github_router

app = FastAPI(title=settings.app_name)
app.include_router(github_router)
app.include_router(repositories_router)
app.include_router(issues_router)
app.include_router(executions_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
