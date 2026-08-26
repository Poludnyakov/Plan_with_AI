from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ReminderPreference(Base):
    """Per unified account reminder policy; kept separate for safe production rollout."""

    __tablename__ = "reminder_preferences"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("unified_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), default="minimal", nullable=False)
    use_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_summary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    summary_hour: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    snooze_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    notification_platform: Mapped[str] = mapped_column(
        String(20), default="telegram", nullable=False
    )
    last_summary_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReminderDeliveryState(Base):
    """Blocks repeated notifications for an event until the user reacts."""

    __tablename__ = "reminder_delivery_states"

    platform: Mapped[str] = mapped_column(String(20), primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("unified_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    awaiting_action: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reminder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
