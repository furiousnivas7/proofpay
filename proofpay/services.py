"""Milestone business logic. Routes call these; nothing here knows about HTTP.

Rules that hold everywhere:
- Every status change goes through `change_status`, which enforces the status flow and writes the timeline.
- Every PayPal call goes through `_paypal`, which logs it.
- A PayPal request ID is committed to the database before the call, so a retry reuses it.
- After a payment call, the result is re-read from PayPal before the milestone status changes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from proofpay.config import Settings
from proofpay.db import utcnow
from proofpay.models import Criterion, Milestone, MilestoneEvent, PayPalCall, Submission, User, Verification
from proofpay.paypal_client import (
    PayPalClient,
    PayPalError,
    approval_link,
    authorization_from_order,
    format_amount,
    new_request_id,
)
from proofpay.states import HOLD_ACTIVE, MilestoneStatus, ensure_transition
from proofpay.verifier import PlaceholderVerifier, Verifier

S = MilestoneStatus


class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class NotFound(ServiceError):
    status_code = 404


class Forbidden(ServiceError):
    status_code = 403


class Conflict(ServiceError):
    status_code = 409


class PaymentFailed(ServiceError):
    status_code = 502


# ---------------------------------------------------------------- helpers


def change_status(session: Session, milestone: Milestone, target: MilestoneStatus, actor: str, message: str) -> None:
    ensure_transition(milestone.status, target)
    session.add(
        MilestoneEvent(
            milestone_id=milestone.id,
            from_status=milestone.status.value,
            to_status=target.value,
            actor=actor,
            message=message,
        )
    )
    milestone.status = target


def add_note(session: Session, milestone: Milestone, actor: str, message: str) -> None:
    session.add(MilestoneEvent(milestone_id=milestone.id, actor=actor, message=message))


def _paypal(
    session: Session, milestone: Milestone | None, operation: str, request_id: str | None, call: Callable[[], Any]
) -> Any:
    log = PayPalCall(milestone_id=milestone.id if milestone else None, operation=operation, request_id=request_id, ok=False)
    session.add(log)
    try:
        result = call()
    except PayPalError as exc:
        log.http_status, log.debug_id, log.error = exc.status_code, exc.debug_id, str(exc)
        session.commit()
        raise PaymentFailed(f"PayPal rejected {operation}: {exc}") from exc
    except httpx.HTTPError as exc:
        log.error = f"{type(exc).__name__}: {exc}"
        session.commit()
        raise PaymentFailed(f"Could not reach PayPal for {operation}; retrying is safe.") from exc
    log.ok = True
    log.http_status = 200
    if isinstance(result, dict):
        log.resource_id = result.get("id")
    session.commit()
    return result


def _parse_paypal_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def require_party(milestone: Milestone, user: User, role: str | None = None) -> None:
    if user.id not in (milestone.client_id, milestone.freelancer_id):
        raise NotFound(f"Milestone {milestone.id} not found")
    if role == "client" and user.id != milestone.client_id:
        raise Forbidden("Only the client can do this")
    if role == "freelancer" and user.id != milestone.freelancer_id:
        raise Forbidden("Only the freelancer can do this")


def get_milestone(session: Session, milestone_id: int, user: User) -> Milestone:
    milestone = session.get(Milestone, milestone_id)
    if milestone is None:
        raise NotFound(f"Milestone {milestone_id} not found")
    require_party(milestone, user)
    return milestone


# ---------------------------------------------------------------- expiry


def expiry_info(milestone: Milestone, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    """When the hold's honor period ends, when it expires, and any warning to show."""
    now = now or utcnow()
    if milestone.authorized_at is None:
        return {"authorized_at": None, "honor_period_ends_at": None, "expires_at": None, "warning": None}
    honor_ends = milestone.authorized_at + timedelta(days=settings.honor_period_days)
    expires = milestone.expires_at or milestone.authorized_at + timedelta(days=settings.authorization_valid_days)
    warning = None
    if milestone.status in HOLD_ACTIVE:
        if now >= expires:
            warning = "The PayPal authorization has expired."
        elif expires - now <= timedelta(days=settings.expiry_warning_days):
            days = max(0, (expires - now).days)
            warning = f"The PayPal authorization expires in {days} day(s). Capture or void it soon."
        elif now >= honor_ends:
            warning = "The 3-day honor period has passed. A capture may now fail if the buyer's funds changed."
    return {
        "authorized_at": milestone.authorized_at,
        "honor_period_ends_at": honor_ends,
        "expires_at": expires,
        "warning": warning,
    }


def refresh_expiry(session: Session, milestone: Milestone, settings: Settings, now: datetime | None = None) -> None:
    """Move a milestone to EXPIRED once its authorization has lapsed."""
    if milestone.status not in HOLD_ACTIVE:
        return
    expires = expiry_info(milestone, settings, now)["expires_at"]
    if expires is not None and (now or utcnow()) >= expires:
        change_status(session, milestone, S.EXPIRED, "system", "The PayPal authorization expired.")
        session.commit()


# ---------------------------------------------------------------- milestone and requirements


def create_milestone(
    session: Session, client: User, freelancer: User, title: str, brief: str, amount: str, settings: Settings
) -> Milestone:
    if client.role != "client":
        raise Forbidden("Only a client can create a milestone")
    try:
        formatted = format_amount(amount)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    milestone = Milestone(
        title=title,
        brief=brief,
        amount=formatted,
        currency=settings.currency,
        status=S.DRAFT,
        client_id=client.id,
        freelancer_id=freelancer.id,
    )
    session.add(milestone)
    session.flush()
    add_note(session, milestone, client.username, f"Milestone created for {formatted} {settings.currency}.")
    session.commit()
    return milestone


def set_criteria(session: Session, milestone: Milestone, user: User, texts: list[str]) -> Milestone:
    require_party(milestone, user, "client")
    if milestone.status != S.DRAFT:
        raise Conflict("Requirements can only change while the milestone is a DRAFT")
    cleaned = [t.strip() for t in texts if t.strip()]
    if not cleaned:
        raise ServiceError("Give at least one requirement")
    milestone.criteria.clear()
    session.flush()
    for position, text in enumerate(cleaned, start=1):
        milestone.criteria.append(Criterion(position=position, text=text))
    milestone.client_approved_criteria = False
    milestone.freelancer_approved_criteria = False
    add_note(session, milestone, user.username, f"Requirements set ({len(cleaned)}). Both sides must approve again.")
    session.commit()
    return milestone


def approve_criteria(session: Session, milestone: Milestone, user: User) -> Milestone:
    require_party(milestone, user)
    if milestone.status != S.DRAFT:
        raise Conflict(f"Requirements are not open for approval (status {milestone.status.value})")
    if not milestone.criteria:
        raise Conflict("There are no requirements to approve yet")
    if user.id == milestone.client_id:
        milestone.client_approved_criteria = True
    else:
        milestone.freelancer_approved_criteria = True
    add_note(session, milestone, user.username, "Approved the requirements.")
    if milestone.client_approved_criteria and milestone.freelancer_approved_criteria:
        change_status(session, milestone, S.CRITERIA_AGREED, user.username, "Both sides agreed the requirements.")
    session.commit()
    return milestone


# ---------------------------------------------------------------- payment: order and authorization


def create_order(session: Session, milestone: Milestone, user: User, paypal: PayPalClient, settings: Settings) -> dict:
    """Create (or reuse) the PayPal order the client approves. Returns the approval URL."""
    require_party(milestone, user, "client")
    if milestone.status != S.CRITERIA_AGREED:
        raise Conflict(f"Payment can only start once requirements are agreed (status {milestone.status.value})")

    if milestone.paypal_order_id:
        order = _paypal(session, milestone, "get_order", None, lambda: paypal.get_order(milestone.paypal_order_id))
        if order.get("status") in ("CREATED", "PAYER_ACTION_REQUIRED", "APPROVED"):
            return {"order_id": order["id"], "status": order["status"], "approval_url": _approval_url(order)}
        milestone.order_request_id = None  # the old order is unusable; start a new one

    if not milestone.order_request_id:
        milestone.order_request_id = new_request_id()
        session.commit()
    base = settings.app_base_url.rstrip("/")
    order = _paypal(
        session,
        milestone,
        "create_order",
        milestone.order_request_id,
        lambda: paypal.create_order(
            amount=milestone.amount,
            currency=milestone.currency,
            description=f"ProofPay milestone #{milestone.id}: {milestone.title}",
            return_url=f"{base}/paypal/return?milestone_id={milestone.id}",
            cancel_url=f"{base}/paypal/cancel?milestone_id={milestone.id}",
            request_id=milestone.order_request_id,
        ),
    )
    milestone.paypal_order_id = order["id"]
    add_note(session, milestone, user.username, f"PayPal order {order['id']} created; waiting for the client to approve it.")
    session.commit()
    return {"order_id": order["id"], "status": order.get("status"), "approval_url": approval_link(order)}


def _approval_url(order: dict) -> str | None:
    try:
        return approval_link(order)
    except ValueError:
        return None


def authorize(session: Session, milestone: Milestone, actor: str, paypal: PayPalClient, settings: Settings) -> Milestone:
    """Place the hold after the client approved the order in PayPal. Safe to call more than once."""
    if milestone.status in HOLD_ACTIVE and milestone.authorization_id:
        return milestone
    if milestone.status != S.CRITERIA_AGREED:
        raise Conflict(f"Cannot authorize a milestone in status {milestone.status.value}")
    if not milestone.paypal_order_id:
        raise Conflict("Create the PayPal order first")

    order_id = milestone.paypal_order_id
    order = _paypal(session, milestone, "get_order", None, lambda: paypal.get_order(order_id))
    if order.get("status") == "APPROVED":
        if not milestone.authorize_request_id:
            milestone.authorize_request_id = new_request_id()
            session.commit()
        order = _paypal(
            session,
            milestone,
            "authorize_order",
            milestone.authorize_request_id,
            lambda: paypal.authorize_order(order_id, request_id=milestone.authorize_request_id),
        )
    elif order.get("status") != "COMPLETED":
        raise Conflict(f"The PayPal order is {order.get('status')}; the client must approve it in PayPal first")

    try:
        authorization_id = authorization_from_order(order)["id"]
    except ValueError as exc:
        raise PaymentFailed(str(exc)) from exc
    confirmed = _paypal(
        session, milestone, "get_authorization", None, lambda: paypal.get_authorization(authorization_id)
    )
    _check_amount(milestone, confirmed.get("amount", {}), "authorized")
    if confirmed.get("status") not in ("CREATED", "PENDING"):
        raise PaymentFailed(f"Authorization {authorization_id} is {confirmed.get('status')}, expected CREATED")

    milestone.authorization_id = authorization_id
    milestone.authorization_status = confirmed["status"]
    milestone.authorized_at = _parse_paypal_time(confirmed.get("create_time")) or utcnow()
    milestone.expires_at = _parse_paypal_time(confirmed.get("expiration_time"))
    change_status(
        session,
        milestone,
        S.AUTHORIZED,
        actor,
        f"PayPal is holding {milestone.amount} {milestone.currency} (authorization {authorization_id}).",
    )
    session.commit()
    return milestone


def _check_amount(milestone: Milestone, amount: dict, what: str) -> None:
    if amount.get("value") != milestone.amount or amount.get("currency_code") != milestone.currency:
        raise PaymentFailed(
            f"PayPal {what} {amount.get('value')} {amount.get('currency_code')}, "
            f"but the milestone is {milestone.amount} {milestone.currency}"
        )


# ---------------------------------------------------------------- work and verification


def submit_work(
    session: Session, milestone: Milestone, user: User, repo_url: str | None, content: str | None, notes: str | None
) -> Submission:
    require_party(milestone, user, "freelancer")
    if not (repo_url or "").strip() and not (content or "").strip():
        raise ServiceError("Give a repository URL or the submitted text")
    ensure_transition(milestone.status, S.SUBMITTED)
    submission = Submission(milestone_id=milestone.id, repo_url=repo_url, content=content, notes=notes)
    session.add(submission)
    session.flush()
    change_status(session, milestone, S.SUBMITTED, user.username, f"Work submitted (submission {submission.id}).")
    session.commit()
    return submission


def verify(session: Session, milestone: Milestone, user: User, verifier: Verifier | None = None) -> Verification:
    """Check the latest submission against the requirements and store the report."""
    require_party(milestone, user)
    ensure_transition(milestone.status, S.VERIFIED)
    if not milestone.submissions:
        raise Conflict("There is no submission to check")
    verifier = verifier or PlaceholderVerifier()
    submission = milestone.submissions[-1]
    report = verifier.check(milestone.criteria, submission)
    verification = Verification(
        milestone_id=milestone.id,
        submission_id=submission.id,
        verifier=verifier.name,
        summary=report.summary,
        results=report.results,
    )
    session.add(verification)
    session.flush()
    change_status(session, milestone, S.VERIFIED, verifier.name, f"Requirement report ready: {report.summary}")
    session.commit()
    return verification


def request_revision(session: Session, milestone: Milestone, user: User, note: str) -> Milestone:
    require_party(milestone, user, "client")
    change_status(session, milestone, S.REVISION_REQUESTED, user.username, f"Changes requested: {note}")
    session.commit()
    return milestone


# ---------------------------------------------------------------- payment: capture and void (client only)


def capture(session: Session, milestone: Milestone, user: User, paypal: PayPalClient) -> Milestone:
    """Release the held payment to the freelancer. Only the client can do this."""
    require_party(milestone, user, "client")
    ensure_transition(milestone.status, S.CAPTURED)
    if not milestone.capture_request_id:
        milestone.capture_request_id = new_request_id()
        session.commit()
    authorization_id = milestone.authorization_id
    result = _paypal(
        session,
        milestone,
        "capture_authorization",
        milestone.capture_request_id,
        lambda: paypal.capture_authorization(authorization_id, request_id=milestone.capture_request_id),
    )
    capture_id = result["id"]
    confirmed = _paypal(session, milestone, "get_capture", None, lambda: paypal.get_capture(capture_id))
    if confirmed.get("status") not in ("COMPLETED", "PENDING"):
        raise PaymentFailed(f"Capture {capture_id} is {confirmed.get('status')}")
    _check_amount(milestone, confirmed.get("amount", {}), "captured")
    milestone.capture_id = capture_id
    milestone.capture_status = confirmed["status"]
    change_status(
        session,
        milestone,
        S.CAPTURED,
        user.username,
        f"Client released {milestone.amount} {milestone.currency}; PayPal capture {capture_id} is {confirmed['status']}.",
    )
    session.commit()
    return milestone


def void(session: Session, milestone: Milestone, user: User, paypal: PayPalClient) -> Milestone:
    """Cancel the hold and return the funds to the client. Only the client can do this."""
    require_party(milestone, user, "client")
    ensure_transition(milestone.status, S.VOIDED)
    if not milestone.void_request_id:
        milestone.void_request_id = new_request_id()
        session.commit()
    authorization_id = milestone.authorization_id
    _paypal(
        session,
        milestone,
        "void_authorization",
        milestone.void_request_id,
        lambda: paypal.void_authorization(authorization_id, request_id=milestone.void_request_id),
    )
    confirmed = _paypal(
        session, milestone, "get_authorization", None, lambda: paypal.get_authorization(authorization_id)
    )
    if confirmed.get("status") != "VOIDED":
        raise PaymentFailed(f"Authorization {authorization_id} is {confirmed.get('status')} after void")
    milestone.authorization_status = "VOIDED"
    change_status(session, milestone, S.VOIDED, user.username, "Client cancelled the PayPal hold.")
    session.commit()
    return milestone


# ---------------------------------------------------------------- what each user may do next


def allowed_actions(milestone: Milestone, user: User) -> list[str]:
    status = milestone.status
    actions: list[str] = []
    is_client = user.id == milestone.client_id
    if status == S.DRAFT:
        if is_client:
            actions.append("set_criteria")
        approved = milestone.client_approved_criteria if is_client else milestone.freelancer_approved_criteria
        if milestone.criteria and not approved:
            actions.append("approve_criteria")
    elif status == S.CRITERIA_AGREED and is_client:
        actions.append("create_order")
        if milestone.paypal_order_id:
            actions.append("authorize")
    if not is_client and status in (S.AUTHORIZED, S.REVISION_REQUESTED):
        actions.append("submit_work")
    if status == S.SUBMITTED:
        actions.append("verify")
    if is_client and status == S.VERIFIED:
        actions += ["capture", "request_revision"]
    if is_client and status in HOLD_ACTIVE:
        actions.append("void")
    return actions
