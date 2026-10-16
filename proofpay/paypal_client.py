"""Minimal PayPal REST client for the ProofPay authorize -> capture/void flow.

Server-side only: the client secret must never reach a browser. Only the
sandbox host is allowed, so the code cannot move real money by accident.
"""

from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    """A non-2xx response from PayPal, with the debug_id PayPal support asks for."""

    def __init__(self, status_code: int, body: Any, debug_id: str | None):
        self.status_code = status_code
        self.body = body
        self.debug_id = debug_id
        name = body.get("name") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else body
        super().__init__(f"PayPal HTTP {status_code} {name or ''}: {message} (debug_id={debug_id})")


def new_request_id() -> str:
    """A fresh PayPal-Request-Id. Reuse the same value only when retrying the same call."""
    return str(uuid.uuid4())


def format_amount(value: str | Decimal) -> str:
    """Validate a positive amount with at most two decimals and format it as PayPal expects."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError(f"Amount must be positive with at most 2 decimals: {value!r}")
    return f"{amount:.2f}"


def approval_link(order: dict) -> str:
    """The URL where the buyer approves the order ("payer-action" or legacy "approve")."""
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link["href"]
    raise ValueError(f"No approval link in order {order.get('id')}")


def authorization_from_order(order: dict) -> dict:
    """The first authorization object in an authorized order."""
    try:
        return order["purchase_units"][0]["payments"]["authorizations"][0]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"No authorization in order {order.get('id')}") from exc


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = SANDBOX_BASE_URL,
        http: httpx.Client | None = None,
    ):
        if not client_id or not client_secret:
            raise ValueError("PayPal client ID and secret are required")
        if base_url.rstrip("/") != SANDBOX_BASE_URL:
            raise ValueError(f"Only the PayPal sandbox is allowed, got {base_url!r}")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._token_expires_at = 0.0

    @classmethod
    def from_env(cls) -> "PayPalClient":
        return cls(
            client_id=os.environ.get("PAYPAL_CLIENT_ID", ""),
            client_secret=os.environ.get("PAYPAL_CLIENT_SECRET", ""),
            base_url=os.environ.get("PAYPAL_BASE_URL", SANDBOX_BASE_URL),
        )

    def _access_token(self) -> str:
        # Refresh a minute early so a token never expires mid-request.
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        response = self._http.post(
            f"{self._base_url}/v1/oauth2/token",
            auth=(self._client_id, self._client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        body = self._parse(response)
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + int(body.get("expires_in", 0))
        return self._token

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = response.text
        if response.is_success:
            return body
        debug_id = body.get("debug_id") if isinstance(body, dict) else None
        raise PayPalError(response.status_code, body, debug_id or response.headers.get("paypal-debug-id"))

    def _request(self, method: str, path: str, json: dict | None = None, request_id: str | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        response = self._http.request(method, f"{self._base_url}{path}", json=json, headers=headers)
        return self._parse(response)

    def create_order(
        self,
        amount: str | Decimal,
        currency: str,
        description: str,
        return_url: str,
        cancel_url: str,
        request_id: str,
    ) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "description": description[:127],
                    "amount": {"currency_code": currency, "value": format_amount(amount)},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "user_action": "PAY_NOW",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }
        return self._request("POST", "/v2/checkout/orders", json=body, request_id=request_id)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def authorize_order(self, order_id: str, request_id: str) -> dict:
        return self._request("POST", f"/v2/checkout/orders/{order_id}/authorize", json={}, request_id=request_id)

    def get_authorization(self, authorization_id: str) -> dict:
        return self._request("GET", f"/v2/payments/authorizations/{authorization_id}")

    def capture_authorization(self, authorization_id: str, request_id: str) -> dict:
        return self._request(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/capture",
            json={"final_capture": True},
            request_id=request_id,
        )

    def get_capture(self, capture_id: str) -> dict:
        return self._request("GET", f"/v2/payments/captures/{capture_id}")

    def void_authorization(self, authorization_id: str, request_id: str) -> dict:
        return self._request("POST", f"/v2/payments/authorizations/{authorization_id}/void", request_id=request_id)

    def verify_webhook_signature(self, headers: dict[str, str], webhook_id: str, event: dict) -> str:
        """Ask PayPal whether a webhook delivery is genuine. Returns "SUCCESS" or "FAILURE"."""
        body = {
            "transmission_id": headers.get("paypal-transmission-id", ""),
            "transmission_time": headers.get("paypal-transmission-time", ""),
            "cert_url": headers.get("paypal-cert-url", ""),
            "auth_algo": headers.get("paypal-auth-algo", ""),
            "transmission_sig": headers.get("paypal-transmission-sig", ""),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        result = self._request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return result.get("verification_status", "FAILURE")
