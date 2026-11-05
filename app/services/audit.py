import uuid

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.users.models import AuditLog


def audit(db: Session, user_id: uuid.UUID | None, action: str, **meta: object) -> None:
    """Record a sensitive action. ``meta`` must contain identifiers and counts only, never
    descriptions, amounts or other financial content."""
    db.add(AuditLog(user_id=user_id, action=action, meta={k: v for k, v in meta.items()}))
    logger.info("audit", extra={"action": action, "user_id": str(user_id) if user_id else None})
