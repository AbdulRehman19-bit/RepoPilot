from fastapi import Depends, FastAPI

from app.api.executions import router as executions_router
from app.api.issues import router as issues_router
from app.api.llm import router as llm_router
from app.api.patches import router as patches_router
from app.api.repositories import router as repositories_router
from app.api.security import verify_token
from app.config import settings
from app.github.router import router as github_router

app = FastAPI(title=settings.app_name)
app.include_router(github_router)
# The webhook authenticates itself with GitHub's signature; every other route needs the API token
# (when one is configured).
for api_router in (
    repositories_router,
    issues_router,
    executions_router,
    llm_router,
    patches_router,
):
    app.include_router(api_router, dependencies=[Depends(verify_token)])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
