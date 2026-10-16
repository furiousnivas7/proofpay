"""Database tables. Amounts are stored as PayPal-formatted strings ("200.00") to avoid float rounding."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from proofpay.db import Base, UTCDateTime, utcnow
from proofpay.states import MilestoneStatus


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16))  # "client" or "freelancer"


class Milestone(Base):
    __tablename__ = "milestones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    brief: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[MilestoneStatus] = mapped_column(
        Enum(MilestoneStatus, native_enum=False, length=32), default=MilestoneStatus.DRAFT
    )
    client_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    freelancer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    client_approved_criteria: Mapped[bool] = mapped_column(Boolean, default=False)
    freelancer_approved_criteria: Mapped[bool] = mapped_column(Boolean, default=False)

    # PayPal state. Each *_request_id is saved before the call so a retry reuses it (idempotency).
    paypal_order_id: Mapped[str | None] = mapped_column(String(64))
    order_request_id: Mapped[str | None] = mapped_column(String(64))
    authorize_request_id: Mapped[str | None] = mapped_column(String(64))
    authorization_id: Mapped[str | None] = mapped_column(String(64), index=True)
    authorization_status: Mapped[str | None] = mapped_column(String(32))
    authorized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    capture_request_id: Mapped[str | None] = mapped_column(String(64))
    capture_id: Mapped[str | None] = mapped_column(String(64), index=True)
    capture_status: Mapped[str | None] = mapped_column(String(32))
    void_request_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    client: Mapped[User] = relationship(foreign_keys=[client_id])
    freelancer: Mapped[User] = relationship(foreign_keys=[freelancer_id])
    criteria: Mapped[list["Criterion"]] = relationship(
        back_populates="milestone", order_by="Criterion.position", cascade="all, delete-orphan"
    )
    submissions: Mapped[list["Submission"]] = relationship(order_by="Submission.id")
    verifications: Mapped[list["Verification"]] = relationship(order_by="Verification.id")
    events: Mapped[list["MilestoneEvent"]] = relationship(order_by="MilestoneEvent.id")
    paypal_calls: Mapped[list["PayPalCall"]] = relationship(order_by="PayPalCall.id")


class Criterion(Base):
    __tablename__ = "criteria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)

    milestone: Mapped[Milestone] = relationship(back_populates="criteria")


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"), index=True)
    repo_url: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Verification(Base):
    __tablename__ = "verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    verifier: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    # [{"criterion_id", "criterion", "result": "pass"|"fail"|"unclear", "reason", "evidence"}]
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MilestoneEvent(Base):
    """One timeline entry: a status change or a note."""

    __tablename__ = "milestone_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PayPalCall(Base):
    """Every call ProofPay makes to PayPal, successful or not."""

    __tablename__ = "paypal_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    milestone_id: Mapped[int | None] = mapped_column(ForeignKey("milestones.id"), index=True)
    operation: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    ok: Mapped[bool] = mapped_column(Boolean)
    http_status: Mapped[int | None] = mapped_column(Integer)
    debug_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WebhookEvent(Base):
    """Verified PayPal webhook events. The unique event_id is how duplicates are ignored."""

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_webhook_events_event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    milestone_id: Mapped[int | None] = mapped_column(ForeignKey("milestones.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
