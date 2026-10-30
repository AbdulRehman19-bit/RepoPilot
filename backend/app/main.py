import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from app.api.executions import router as executions_router
from app.api.issues import router as issues_router
from app.api.llm import router as llm_router
from app.api.patches import router as patches_router
from app.api.repositories import router as repositories_router
from app.api.security import get_principal
from app.auth.router import router as auth_router
from app.config import settings
from app.github.router import router as github_router
from app.security.limits import BodyLimitMiddleware

logger = logging.getLogger(__name__)
if not settings.named_tokens() and not settings.github_client_id:
    logger.warning(
        "Neither REPOPILOT_GITHUB_CLIENT_ID nor REPOPILOT_API_TOKEN is set: the API is open and "
        "approvals are disabled. Fine for local development, never for anything reachable from "
        "a network."
    )

app = FastAPI(title=settings.app_name)


app.add_middleware(BodyLimitMiddleware)


@app.exception_handler(ClientDisconnect)
async def body_cut_off(request: Request, exc: ClientDisconnect) -> JSONResponse:
    """The body limit stopped the upload mid-read: answer as the refusal it was, not a crash."""
    return JSONResponse({"detail": "request body too large"}, status_code=413)


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
