from fastapi import APIRouter
from pydantic import BaseModel

from app.llm.factory import get_provider

router = APIRouter(prefix="/llm", tags=["llm"])


class LLMStatus(BaseModel):
    provider: str
    model: str | None
    available: bool
    detail: str


@router.get("/status")
def status() -> LLMStatus:
    provider = get_provider()
    if provider is None:
        return LLMStatus(provider="none", model=None, available=False, detail="model disabled")
    ok, detail = provider.available()
    return LLMStatus(provider=provider.name, model=provider.model, available=ok, detail=detail)
