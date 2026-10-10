from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class CalendarImport(Base):
    """One import from another calendar: an .ics file, an iCal link or CalDAV."""

    __tablename__ = "calendar_imports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("unified_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    external_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # file, link, caldav
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="imported", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CalendarImportItem(Base):
    """An event created by an import; the key skips it when imported again."""

    __tablename__ = "calendar_import_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("calendar_imports.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # telegram, max
    event_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    uid_key: Mapped[str] = mapped_column(String(600), index=True, nullable=False)
