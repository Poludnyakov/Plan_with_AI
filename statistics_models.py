from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class StatisticsBaseline(Base):
    """One-time snapshot separating historical counters from live DB growth."""

    __tablename__ = "statistics_baseline"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actual_users_at_start: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_events_at_start: Mapped[int] = mapped_column(Integer, nullable=False)
    historical_users: Mapped[int] = mapped_column(Integer, default=527, nullable=False)
    historical_events: Mapped[int] = mapped_column(Integer, default=7451, nullable=False)
    historical_week3_retention: Mapped[float] = mapped_column(
        Float, default=35.0, nullable=False
    )
