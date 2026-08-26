from datetime import date, datetime, time

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, JSON, LargeBinary, String, Text, Time, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class ScheduleSeries(Base):
    """A recurring background calendar item, separate from personal events."""

    __tablename__ = "schedule_series"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("unified_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    valid_until: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    week_pattern: Mapped[str] = mapped_column(String(12), default="every", nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), default="Europe/Moscow", nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="schedule_photo", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    exceptions: Mapped[list["ScheduleException"]] = relationship(
        back_populates="series", cascade="all, delete-orphan", passive_deletes=True
    )


class ScheduleException(Base):
    __tablename__ = "schedule_exceptions"
    __table_args__ = (
        UniqueConstraint("series_id", "occurrence_date", name="uq_schedule_exception_occurrence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    series_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schedule_series.id", ondelete="CASCADE"), index=True, nullable=False
    )
    occurrence_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="skipped", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    series: Mapped[ScheduleSeries] = relationship(back_populates="exceptions")


class ScheduleImportDraft(Base):
    """Persisted import dialogue so restarts do not lose a recognized schedule."""

    __tablename__ = "schedule_import_drafts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("unified_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_platform: Mapped[str] = mapped_column(String(20), nullable=False)
    slots: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="awaiting_range", index=True, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScheduleImportSource(Base):
    """Temporary original file retained while the user supplies import instructions."""

    __tablename__ = "schedule_import_sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("unified_accounts.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    source_platform: Mapped[str] = mapped_column(String(20), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default="awaiting_input", index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
