"""Persisted computation results (forecasts, simulations) and agent conversations."""

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base
from app.database.types import IdMixin, JsonType, TimestampMixin


class Forecast(IdMixin, TimestampMixin, Base):
    __tablename__ = "forecasts"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    request: Mapped[dict] = mapped_column(JsonType, default=dict)
    result: Mapped[dict] = mapped_column(JsonType, default=dict)


class PortfolioSimulation(IdMixin, TimestampMixin, Base):
    __tablename__ = "portfolio_simulations"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    request: Mapped[dict] = mapped_column(JsonType, default=dict)
    result: Mapped[dict] = mapped_column(JsonType, default=dict)


class Conversation(IdMixin, TimestampMixin, Base):
    __tablename__ = "agent_conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="")


class Message(IdMixin, TimestampMixin, Base):
    __tablename__ = "agent_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JsonType, default=dict)  # sources, assumptions, tool calls
