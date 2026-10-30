import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.executions import router as executions_router
from app.api.issues import router as issues_router
from app.api.llm import router as llm_router
from app.api.patches import router as patches_router
from app.api.repositories import router as repositories_router
from app.api.security import get_principal
from app.auth.router import router as auth_router
from app.config import settings
from app.github.router import router as github_router

logger = logging.getLogger(__name__)
if not settings.named_tokens() and not settings.github_client_id:
    logger.warning(
        "Neither REPOPILOT_GITHUB_CLIENT_ID nor REPOPILOT_API_TOKEN is set: the API is open and "
        "approvals are disabled. Fine for local development, never for anything reachable from "
        "a network."
    )

app = FastAPI(title=settings.app_name)


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Refuse oversized API bodies up front. (Webhooks enforce their own, larger, cap.)"""
    declared = request.headers.get("content-length")
    too_big = declared is not None and (
        not declared.isdigit() or int(declared) > settings.max_request_bytes
    )
    if too_big and not request.url.path.startswith("/github/"):
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    return await call_next(request)


app.include_router(github_router)
app.include_router(auth_router)
# The webhook authenticates itself with GitHub's signature, /auth is the sign-in flow; every other
# route needs a signed-in user or the API token (unless nothing is configured: local development).
for api_router in (
    repositories_router,
    issues_router,
    executions_router,
    llm_router,
    patches_router,
):
    app.include_router(api_router, dependencies=[Depends(get_principal)])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
