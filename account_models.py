from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class UnifiedAccount(Base):
    __tablename__ = "unified_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    identities: Mapped[list["AccountIdentity"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    preference: Mapped["AccountPreference"] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )


class AccountIdentity(Base):
    __tablename__ = "account_identities"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_account_identity_platform_id"),
        UniqueConstraint("account_id", "platform", name="uq_account_one_identity_per_platform"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("unified_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    platform: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    external_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    account: Mapped[UnifiedAccount] = relationship(back_populates="identities")


class AccountPreference(Base):
    __tablename__ = "account_preferences"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("unified_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    intelligent_reminders: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    account: Mapped[UnifiedAccount] = relationship(back_populates="preference")


class AccountLinkCode(Base):
    __tablename__ = "account_link_codes"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    source_identity_id: Mapped[int] = mapped_column(
        ForeignKey("account_identities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebLoginTicket(Base):
    __tablename__ = "web_login_tickets"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    short_code: Mapped[str] = mapped_column(String(12), unique=True, index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("unified_accounts.id", ondelete="CASCADE"), index=True, nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

