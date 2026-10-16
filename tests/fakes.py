"""An in-memory stand-in for PayPalClient that mimics the sandbox closely enough for API tests."""

from __future__ import annotations

import copy
import itertools
from datetime import datetime, timedelta, timezone

import httpx

from proofpay.paypal_client import PayPalError


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakePayPal:
    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.calls: list[tuple[str, str | None]] = []
        self.webhook_status = "SUCCESS"
        self.authorized_amount_override: str | None = None
        self._seen: dict[tuple[str, str], dict] = {}  # (operation, request_id) -> first response
        self._ids = itertools.count(1)
        self._lose_response: set[str] = set()

    # ---- test controls
    def approve(self, order_id: str) -> None:
        """What the sandbox buyer does in the browser."""
        self.orders[order_id]["status"] = "APPROVED"

    def lose_response_next(self, operation: str) -> None:
        """Make PayPal do the work but the response never arrives (a network failure)."""
        self._lose_response.add(operation)

    # ---- helpers
    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):06d}"

    def _idempotent(self, operation: str, request_id: str | None, make):
        self.calls.append((operation, request_id))
        key = (operation, request_id or "")
        if request_id and key in self._seen:
            result = self._seen[key]
        else:
            result = make()
            if request_id:
                self._seen[key] = result
        if operation in self._lose_response:
            self._lose_response.discard(operation)
            raise httpx.ConnectError("connection reset")
        return copy.deepcopy(result)

    @staticmethod
    def _error(status: int, name: str) -> PayPalError:
        return PayPalError(status, {"name": name, "message": name}, "fake-debug-id")

    def _order_view(self, order_id: str) -> dict:
        order = self.orders.get(order_id)
        if order is None:
            raise self._error(404, "RESOURCE_NOT_FOUND")
        view = {
            "id": order_id,
            "status": order["status"],
            "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"}],
            "purchase_units": [{"amount": dict(order["amount"])}],
        }
        if order.get("authorization_id"):
            view["purchase_units"][0]["payments"] = {
                "authorizations": [copy.deepcopy(self.authorizations[order["authorization_id"]])]
            }
        return view

    # ---- PayPalClient interface
    def create_order(self, amount, currency, description, return_url, cancel_url, request_id):
        def make():
            order_id = self._id("ORDER")
            self.orders[order_id] = {
                "status": "PAYER_ACTION_REQUIRED",
                "amount": {"currency_code": currency, "value": str(amount)},
                "return_url": return_url,
            }
            return self._order_view(order_id)

        return self._idempotent("create_order", request_id, make)

    def get_order(self, order_id):
        self.calls.append(("get_order", None))
        return self._order_view(order_id)

    def authorize_order(self, order_id, request_id):
        def make():
            order = self.orders[order_id]
            if order["status"] != "APPROVED":
                raise self._error(422, "ORDER_NOT_APPROVED")
            now = datetime.now(timezone.utc)
            auth_id = self._id("AUTH")
            amount = dict(order["amount"])
            if self.authorized_amount_override:
                amount["value"] = self.authorized_amount_override
            self.authorizations[auth_id] = {
                "id": auth_id,
                "status": "CREATED",
                "amount": amount,
                "create_time": _iso(now),
                "expiration_time": _iso(now + timedelta(days=29)),
            }
            order["status"] = "COMPLETED"
            order["authorization_id"] = auth_id
            return self._order_view(order_id)

        return self._idempotent("authorize_order", request_id, make)

    def get_authorization(self, authorization_id):
        self.calls.append(("get_authorization", None))
        if authorization_id not in self.authorizations:
            raise self._error(404, "RESOURCE_NOT_FOUND")
        return copy.deepcopy(self.authorizations[authorization_id])

    def capture_authorization(self, authorization_id, request_id):
        def make():
            auth = self.authorizations[authorization_id]
            if auth["status"] != "CREATED":
                raise self._error(422, "AUTHORIZATION_ALREADY_COMPLETED")
            capture_id = self._id("CAP")
            self.captures[capture_id] = {"id": capture_id, "status": "COMPLETED", "amount": dict(auth["amount"])}
            auth["status"] = "CAPTURED"
            return copy.deepcopy(self.captures[capture_id])

        return self._idempotent("capture_authorization", request_id, make)

    def get_capture(self, capture_id):
        self.calls.append(("get_capture", None))
        return copy.deepcopy(self.captures[capture_id])

    def void_authorization(self, authorization_id, request_id):
        def make():
            auth = self.authorizations[authorization_id]
            if auth["status"] != "CREATED":
                raise self._error(422, "AUTHORIZATION_ALREADY_COMPLETED")
            auth["status"] = "VOIDED"
            return {}

        return self._idempotent("void_authorization", request_id, make)

    def verify_webhook_signature(self, headers, webhook_id, event):
        self.calls.append(("verify_webhook_signature", None))
        return self.webhook_status
