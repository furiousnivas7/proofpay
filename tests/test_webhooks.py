"""Webhook signature checks, duplicate handling and the status updates they trigger."""

from sqlalchemy import func, select

from proofpay.models import WebhookEvent
from tests.conftest import CLIENT


def event(event_id, event_type, resource):
    return {"id": event_id, "event_type": event_type, "resource": resource}


def count_events(session_factory):
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(WebhookEvent))


def test_rejects_bad_signature(api, paypal, session_factory):
    paypal.webhook_status = "FAILURE"
    response = api.post("/webhooks/paypal", json=event("WH-1", "PAYMENT.CAPTURE.COMPLETED", {"id": "X"}))
    assert response.status_code == 400
    assert count_events(session_factory) == 0


def test_rejects_when_webhook_id_not_configured(api, settings):
    settings.paypal_webhook_id = ""
    response = api.post("/webhooks/paypal", json=event("WH-1", "PAYMENT.CAPTURE.COMPLETED", {"id": "X"}))
    assert response.status_code == 400


def test_rejects_non_json(api):
    assert api.post("/webhooks/paypal", content=b"not json").status_code == 400


def test_voided_authorization_event_voids_the_milestone_once(flow, api, paypal, session_factory):
    m = flow.to("AUTHORIZED")
    auth_id = m["paypal"]["authorization_id"]
    paypal.authorizations[auth_id]["status"] = "VOIDED"  # voided outside ProofPay
    body = event("WH-2", "PAYMENT.AUTHORIZATION.VOIDED", {"id": auth_id, "status": "VOIDED"})

    assert api.post("/webhooks/paypal", json=body).json() == {"result": "processed"}
    assert api.post("/webhooks/paypal", json=body).json() == {"result": "duplicate"}
    assert count_events(session_factory) == 1
    assert flow.get()["status"] == "VOIDED"


def test_expired_authorization_event_expires_the_milestone(flow, api, paypal):
    m = flow.to("SUBMITTED")
    auth_id = m["paypal"]["authorization_id"]
    paypal.authorizations[auth_id]["status"] = "EXPIRED"
    api.post("/webhooks/paypal", json=event("WH-3", "PAYMENT.AUTHORIZATION.VOIDED", {"id": auth_id}))
    assert flow.get()["status"] == "EXPIRED"


def test_webhook_is_checked_against_paypal_not_trusted(flow, api, paypal):
    m = flow.to("AUTHORIZED")
    auth_id = m["paypal"]["authorization_id"]  # PayPal still says CREATED
    api.post("/webhooks/paypal", json=event("WH-4", "PAYMENT.AUTHORIZATION.VOIDED", {"id": auth_id}))
    assert flow.get()["status"] == "AUTHORIZED"


def test_capture_event_updates_capture_status(flow, api):
    flow.to("VERIFIED")
    m = flow.ok(api.post(f"/milestones/{flow.id}/payment/capture", auth=CLIENT))
    capture_id = m["paypal"]["capture_id"]
    api.post("/webhooks/paypal", json=event("WH-5", "PAYMENT.CAPTURE.PENDING", {"id": capture_id, "status": "PENDING"}))
    assert flow.get()["paypal"]["capture_status"] == "PENDING"
