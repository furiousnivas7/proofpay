"""The milestone flow end to end through the HTTP API, with a fake PayPal."""

from tests.conftest import CLIENT, FREELANCER


def test_happy_path_draft_to_captured(flow, api, paypal):
    assert flow.create()["status"] == "DRAFT"
    assert flow.agree_criteria()["status"] == "CRITERIA_AGREED"

    m = flow.authorize()
    assert m["status"] == "AUTHORIZED"
    assert m["paypal"]["authorization_id"].startswith("AUTH")
    assert m["expiry"]["expires_at"] and m["expiry"]["honor_period_ends_at"]
    assert m["expiry"]["warning"] is None

    assert flow.submit()["status"] == "SUBMITTED"
    m = flow.verify()
    assert m["status"] == "VERIFIED"
    assert [r["result"] for r in m["latest_report"]["results"]] == ["unclear", "unclear"]
    assert set(flow.get(CLIENT)["your_actions"]) == {"capture", "request_revision", "void"}

    m = flow.ok(api.post(f"/milestones/{flow.id}/payment/capture", auth=CLIENT))
    assert m["status"] == "CAPTURED"
    assert m["paypal"]["capture_status"] == "COMPLETED"
    assert m["your_actions"] == []

    timeline = flow.ok(api.get(f"/milestones/{flow.id}/timeline", auth=FREELANCER))
    statuses = [e["to_status"] for e in timeline if e["kind"] == "status"]
    assert statuses == ["CRITERIA_AGREED", "AUTHORIZED", "SUBMITTED", "VERIFIED", "CAPTURED"]
    operations = [e["operation"] for e in timeline if e["kind"] == "paypal_call"]
    assert "capture_authorization" in operations and "get_capture" in operations


def test_void_returns_the_hold(flow, api, paypal):
    m = flow.to("SUBMITTED")
    m = flow.ok(api.post(f"/milestones/{flow.id}/payment/void", auth=CLIENT))
    assert m["status"] == "VOIDED"
    assert paypal.authorizations[m["paypal"]["authorization_id"]]["status"] == "VOIDED"


def test_revision_then_resubmit(flow, api):
    flow.to("VERIFIED")
    m = flow.ok(api.post(f"/milestones/{flow.id}/revision", json={"note": "Form is broken"}, auth=CLIENT))
    assert m["status"] == "REVISION_REQUESTED"
    assert flow.get(FREELANCER)["your_actions"] == ["submit_work"]
    assert flow.submit()["status"] == "SUBMITTED"
    assert len(flow.get()["submissions"]) == 2


def test_capture_retry_after_lost_response_captures_once(flow, api, paypal):
    flow.to("VERIFIED")
    paypal.lose_response_next("capture_authorization")
    first = api.post(f"/milestones/{flow.id}/payment/capture", auth=CLIENT)
    assert first.status_code == 502
    assert "retrying is safe" in first.json()["detail"]
    assert flow.get()["status"] == "VERIFIED"

    m = flow.ok(api.post(f"/milestones/{flow.id}/payment/capture", auth=CLIENT))
    assert m["status"] == "CAPTURED"
    assert len(paypal.captures) == 1
    capture_request_ids = {rid for op, rid in paypal.calls if op == "capture_authorization"}
    assert len(capture_request_ids) == 1


def test_create_order_twice_reuses_the_order(flow, api):
    flow.to("CRITERIA_AGREED")
    first = flow.ok(api.post(f"/milestones/{flow.id}/payment/order", auth=CLIENT))
    second = flow.ok(api.post(f"/milestones/{flow.id}/payment/order", auth=CLIENT))
    assert first["order_id"] == second["order_id"]
    assert first["approval_url"].startswith("https://www.sandbox.paypal.com/")


def test_authorize_before_buyer_approval_is_refused(flow, api):
    flow.to("CRITERIA_AGREED")
    flow.ok(api.post(f"/milestones/{flow.id}/payment/order", auth=CLIENT))
    response = api.post(f"/milestones/{flow.id}/payment/authorize", auth=CLIENT)
    assert response.status_code == 409
    assert flow.get()["status"] == "CRITERIA_AGREED"


def test_authorized_amount_mismatch_is_refused(flow, api, paypal):
    flow.to("CRITERIA_AGREED")
    order = flow.ok(api.post(f"/milestones/{flow.id}/payment/order", auth=CLIENT))
    paypal.approve(order["order_id"])
    paypal.authorized_amount_override = "1.00"
    response = api.post(f"/milestones/{flow.id}/payment/authorize", auth=CLIENT)
    assert response.status_code == 502
    assert flow.get()["status"] == "CRITERIA_AGREED"


def test_paypal_return_with_wrong_token_does_nothing(flow, api):
    flow.to("CRITERIA_AGREED")
    flow.ok(api.post(f"/milestones/{flow.id}/payment/order", auth=CLIENT))
    assert api.get(f"/paypal/return?milestone_id={flow.id}&token=NOT-THE-ORDER").status_code == 404
    assert flow.get()["status"] == "CRITERIA_AGREED"


def test_capture_before_report_is_blocked(flow, api):
    flow.to("SUBMITTED")
    response = api.post(f"/milestones/{flow.id}/payment/capture", auth=CLIENT)
    assert response.status_code == 409
    assert "SUBMITTED to CAPTURED" in response.json()["detail"]


def test_changing_requirements_resets_approvals(flow, api):
    flow.create()
    flow.ok(api.put(f"/milestones/{flow.id}/criteria", json={"criteria": ["A"]}, auth=CLIENT))
    flow.ok(api.post(f"/milestones/{flow.id}/criteria/approve", auth=FREELANCER))
    m = flow.ok(api.put(f"/milestones/{flow.id}/criteria", json={"criteria": ["A", "B"]}, auth=CLIENT))
    assert m["criteria_approved_by"] == {"client": False, "freelancer": False}
    assert m["status"] == "DRAFT"


def test_invalid_amount_is_rejected(api):
    response = api.post("/milestones", json={"title": "x", "amount": "10.001"}, auth=CLIENT)
    assert response.status_code == 400


# ---------------------------------------------------------------- roles


def test_wrong_password_is_rejected(api):
    assert api.get("/me", auth=("client", "nope")).status_code == 401
    assert api.get("/me", auth=("stranger", "client-demo")).status_code == 401
    assert api.get("/me", auth=CLIENT).json() == {"username": "client", "role": "client"}


def test_freelancer_cannot_do_client_actions(flow, api):
    flow.to("VERIFIED")
    for path in ("payment/capture", "payment/void", "payment/order"):
        assert api.post(f"/milestones/{flow.id}/{path}", auth=FREELANCER).status_code == 403, path
    assert api.post(f"/milestones/{flow.id}/revision", json={"note": "x"}, auth=FREELANCER).status_code == 403
    assert api.post("/milestones", json={"title": "x", "amount": "5.00"}, auth=FREELANCER).status_code == 403


def test_client_cannot_submit_work(flow, api):
    flow.to("AUTHORIZED")
    response = api.post(f"/milestones/{flow.id}/submissions", json={"content": "hi"}, auth=CLIENT)
    assert response.status_code == 403


def test_each_role_sees_only_its_own_actions(flow):
    flow.to("AUTHORIZED")
    assert flow.get(CLIENT)["your_actions"] == ["void"]
    assert flow.get(FREELANCER)["your_actions"] == ["submit_work"]
