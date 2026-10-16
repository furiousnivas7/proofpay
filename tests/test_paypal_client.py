import json

import httpx
import pytest

from proofpay.paypal_client import (
    SANDBOX_BASE_URL,
    PayPalClient,
    PayPalError,
    approval_link,
    authorization_from_order,
    format_amount,
)


def make_client(handler):
    return PayPalClient("id", "secret", http=httpx.Client(transport=httpx.MockTransport(handler)))


def token_response():
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 32400})


def test_refuses_non_sandbox_host():
    with pytest.raises(ValueError, match="sandbox"):
        PayPalClient("id", "secret", base_url="https://api-m.paypal.com")


def test_requires_credentials():
    with pytest.raises(ValueError):
        PayPalClient("", "secret")


def test_token_is_cached_and_request_id_sent():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        return httpx.Response(201, json={"id": "CAP1", "status": "COMPLETED"})

    client = make_client(handler)
    client.capture_authorization("AUTH1", request_id="rid-1")
    client.capture_authorization("AUTH1", request_id="rid-1")

    assert [c.url.path for c in calls].count("/v1/oauth2/token") == 1
    capture_calls = [c for c in calls if c.url.path.endswith("/capture")]
    assert all(c.headers["PayPal-Request-Id"] == "rid-1" for c in capture_calls)
    assert all(c.headers["Authorization"] == "Bearer tok" for c in capture_calls)
    assert json.loads(capture_calls[0].content) == {"final_capture": True}


def test_create_order_body():
    seen = {}

    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ORDER1", "status": "PAYER_ACTION_REQUIRED"})

    make_client(handler).create_order("20", "USD", "Milestone 1", "https://r", "https://c", request_id="rid")

    assert seen["body"]["intent"] == "AUTHORIZE"
    assert seen["body"]["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "20.00"}


def test_error_carries_debug_id():
    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "message": "nope", "debug_id": "dbg123"})

    with pytest.raises(PayPalError) as excinfo:
        make_client(handler).get_order("ORDER1")
    assert excinfo.value.status_code == 422
    assert excinfo.value.debug_id == "dbg123"


def test_void_handles_empty_204():
    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return token_response()
        return httpx.Response(204)

    assert make_client(handler).void_authorization("AUTH1", request_id="rid") == {}


@pytest.mark.parametrize("value,expected", [("20", "20.00"), ("19.5", "19.50"), ("0.01", "0.01")])
def test_format_amount_valid(value, expected):
    assert format_amount(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "1.234", "abc"])
def test_format_amount_invalid(value):
    with pytest.raises(ValueError):
        format_amount(value)


def test_approval_link_and_authorization_helpers():
    order = {
        "id": "O1",
        "links": [{"rel": "self", "href": "s"}, {"rel": "payer-action", "href": "https://approve"}],
        "purchase_units": [{"payments": {"authorizations": [{"id": "A1", "status": "CREATED"}]}}],
    }
    assert approval_link(order) == "https://approve"
    assert authorization_from_order(order)["id"] == "A1"
    with pytest.raises(ValueError):
        approval_link({"id": "O2", "links": []})


def test_sandbox_base_url_constant():
    assert SANDBOX_BASE_URL == "https://api-m.sandbox.paypal.com"
