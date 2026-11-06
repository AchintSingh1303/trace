import asyncio

from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..models import User
from ..schemas import LLMCheckBody
from ..security import get_current_user, require_role
from ..services import llm

router = APIRouter(prefix="/api/llm", tags=["generators"])


@router.get("/providers")
def providers(_: User = Depends(get_current_user)):
    """What the generator picker offers. Never returns a key, only whether the server has one."""
    server_default = llm.normalise_provider(settings.llm_provider)
    items = [
        {"id": "rules", "label": "Built-in rules (offline, no key)", "default_model": "", "key_hint": "", "server_key": False}
    ]
    for pid, info in llm.PROVIDERS.items():
        items.append(
            {
                "id": pid,
                "label": info["label"],
                "default_model": info["default_model"],
                "key_hint": info["key_hint"],
                "server_key": bool(settings.llm_api_key) and server_default == pid,
            }
        )
    return {"default": server_default if server_default in {"rules", *llm.PROVIDERS} else "rules", "providers": items}


@router.post("/check")
async def check(body: LLMCheckBody, _: User = Depends(require_role("qa_lead"))):
    """Make one tiny request so a person can confirm their key works before a long run."""
    cfg = llm.resolve_config(body.provider, body.api_key, body.model)
    try:
        return await asyncio.to_thread(llm.check_connection, cfg)
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
