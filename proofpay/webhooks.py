"""PayPal webhook handling: verify the signature with PayPal, ignore duplicates, update the milestone."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from proofpay.config import Settings
from proofpay.models import Milestone, WebhookEvent
from proofpay.paypal_client import PayPalClient
from proofpay.services import ServiceError, _paypal, add_note, change_status, refresh_expiry
from proofpay.states import HOLD_ACTIVE, MilestoneStatus

S = MilestoneStatus


class WebhookRejected(ServiceError):
    status_code = 400


def handle_webhook(
    session: Session, paypal: PayPalClient, settings: Settings, headers: dict[str, str], event: dict[str, Any]
) -> str:
    """Returns "processed" or "duplicate". Raises WebhookRejected for unverified deliveries."""
    if not settings.paypal_webhook_id:
        raise WebhookRejected("PAYPAL_WEBHOOK_ID is not set, so webhooks cannot be verified")
    event_id = event.get("id")
    if not event_id or not event.get("event_type"):
        raise WebhookRejected("Not a PayPal webhook event")
    if session.scalar(select(WebhookEvent.id).where(WebhookEvent.event_id == event_id)) is not None:
        return "duplicate"

    status = _paypal(
        session,
        None,
        "verify_webhook_signature",
        None,
        lambda: paypal.verify_webhook_signature(headers, settings.paypal_webhook_id, event),
    )
    if status != "SUCCESS":
        raise WebhookRejected(f"PayPal says the webhook signature is invalid ({status})")

    resource = event.get("resource") or {}
    milestone = _find_milestone(session, resource)
    record = WebhookEvent(
        event_id=event_id,
        event_type=event["event_type"],
        resource_id=resource.get("id"),
        milestone_id=milestone.id if milestone else None,
        payload=event,
    )
    session.add(record)
    try:
        session.flush()
    except IntegrityError:  # the same event arrived twice at the same moment
        session.rollback()
        return "duplicate"

    if milestone is not None:
        _apply(session, paypal, settings, milestone, event["event_type"], resource)
    session.commit()
    return "processed"


def _find_milestone(session: Session, resource: dict[str, Any]) -> Milestone | None:
    ids = {resource.get("id")}
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    ids.update({related.get("authorization_id"), related.get("order_id")})
    ids.discard(None)
    if not ids:
        return None
    return session.scalar(
        select(Milestone).where(
            or_(
                Milestone.authorization_id.in_(ids),
                Milestone.capture_id.in_(ids),
                Milestone.paypal_order_id.in_(ids),
            )
        )
    )


def _apply(
    session: Session,
    paypal: PayPalClient,
    settings: Settings,
    milestone: Milestone,
    event_type: str,
    resource: dict[str, Any],
) -> None:
    add_note(session, milestone, "paypal-webhook", f"PayPal event {event_type} for {resource.get('id')}.")

    if event_type == "PAYMENT.AUTHORIZATION.VOIDED" and milestone.status in HOLD_ACTIVE:
        # The hold ended outside ProofPay (for example it expired). Confirm with PayPal before acting.
        authorization_id = milestone.authorization_id
        confirmed = _paypal(
            session, milestone, "get_authorization", None, lambda: paypal.get_authorization(authorization_id)
        )
        paypal_status = confirmed.get("status")
        milestone.authorization_status = paypal_status
        if paypal_status == "EXPIRED":
            change_status(session, milestone, S.EXPIRED, "paypal-webhook", "PayPal reports the authorization expired.")
        elif paypal_status == "VOIDED":
            change_status(session, milestone, S.VOIDED, "paypal-webhook", "PayPal reports the authorization was voided.")

    elif event_type.startswith("PAYMENT.CAPTURE.") and resource.get("id") == milestone.capture_id:
        milestone.capture_status = resource.get("status") or milestone.capture_status
        if event_type == "PAYMENT.CAPTURE.DENIED":
            add_note(session, milestone, "paypal-webhook", "Warning: PayPal denied the capture.")

    refresh_expiry(session, milestone, settings)
