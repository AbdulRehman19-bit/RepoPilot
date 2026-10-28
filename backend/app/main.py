from fastapi import FastAPI

from app.config import settings
from app.github.router import router as github_router

app = FastAPI(title=settings.app_name)
app.include_router(github_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
