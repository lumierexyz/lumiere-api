import uuid

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.ai import agent
from app.auth.deps import CurrentProfile, DbSession
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.rate_limit import limiter
from app.database.results import Conversation, Message

router = APIRouter(prefix="/agent", tags=["agent"])


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None


@router.post("/chat")
def chat(body: ChatIn, profile: CurrentProfile, db: DbSession) -> dict:
    limiter.check(f"agent:{profile.id}", get_settings().agent_rate_limit_per_minute)
    return agent.chat(db, profile, body.message.strip(), body.conversation_id)


@router.get("/conversations")
def list_conversations(
    profile: CurrentProfile, db: DbSession, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)
) -> dict:
    q = select(Conversation).where(Conversation.user_id == profile.id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Conversation.created_at.desc()).limit(limit).offset(offset))
    return {
        "items": [{"id": str(c.id), "title": c.title, "created_at": c.created_at.isoformat()} for c in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: uuid.UUID, profile: CurrentProfile, db: DbSession) -> dict:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != profile.id:
        raise NotFoundError("Conversation not found.")
    msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at))
    return {
        "id": str(conv.id),
        "title": conv.title,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
                **(
                    {"sources": m.meta.get("sources"), "assumptions": m.meta.get("assumptions")}
                    if m.role == "assistant"
                    else {}
                ),
            }
            for m in msgs
        ],
    }


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: uuid.UUID, profile: CurrentProfile, db: DbSession) -> None:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != profile.id:
        raise NotFoundError("Conversation not found.")
    db.delete(conv)
    db.commit()
