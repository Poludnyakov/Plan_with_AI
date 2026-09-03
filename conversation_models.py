from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ConversationTurn(Base):
    """A short, account-scoped dialogue memory shared by Telegram and MAX."""

    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("unified_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    platform: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    event_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
