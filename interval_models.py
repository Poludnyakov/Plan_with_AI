from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class EventTiming(Base):
    """Start/end interval attached to a legacy Event row."""

    __tablename__ = "event_timings"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
