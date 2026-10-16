"""Day-1 check: can this PayPal Sandbox app do the ProofPay payment flow?

Flow 1 (capture): create order -> buyer approves -> authorize -> capture,
    replay the capture with the same PayPal-Request-Id (idempotency),
    then confirm the capture server-side.
Flow 2 (void):    create order -> buyer approves -> authorize -> void,
    then confirm the authorization is VOIDED server-side.

Usage:
    cp .env.example .env   # fill in sandbox credentials
    python scripts/sandbox_check.py              # both flows
    python scripts/sandbox_check.py --flow capture
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from proofpay.paypal_client import (  # noqa: E402
    PayPalClient,
    PayPalError,
    approval_link,
    authorization_from_order,
    format_amount,
    new_request_id,
)

RETURN_URL = "https://example.com/proofpay/return"
CANCEL_URL = "https://example.com/proofpay/cancel"


class CheckFailed(Exception):
    pass


def check(condition: bool, label: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise CheckFailed(label)


def create_and_authorize(client: PayPalClient, amount: str, currency: str, label: str) -> dict:
    order = client.create_order(
        amount=amount,
        currency=currency,
        description=f"ProofPay sandbox check ({label})",
        return_url=RETURN_URL,
        cancel_url=CANCEL_URL,
        request_id=new_request_id(),
    )
    check(order.get("status") == "PAYER_ACTION_REQUIRED", f"order {order['id']} created, waiting for buyer")

    print("\n  Open this link, log in with your SANDBOX PERSONAL (buyer) account and approve:")
    print(f"  {approval_link(order)}")
    print("  (After approving you'll land on example.com - that's expected.)")
    input("  Press Enter once you have approved... ")

    order = client.get_order(order["id"])
    check(order.get("status") == "APPROVED", f"order status is APPROVED (got {order.get('status')})")

    order = client.authorize_order(order["id"], request_id=new_request_id())
    authorization = authorization_from_order(order)
    check(authorization.get("status") == "CREATED", f"authorization {authorization['id']} created")
    print(f"  Authorization expires: {authorization.get('expiration_time')}")
    return authorization


def capture_flow(client: PayPalClient, amount: str, currency: str) -> None:
    print("\n== Flow 1: authorize -> capture ==")
    authorization = create_and_authorize(client, amount, currency, "capture")

    request_id = new_request_id()
    capture = client.capture_authorization(authorization["id"], request_id=request_id)
    check(capture.get("status") in ("COMPLETED", "PENDING"), f"capture {capture['id']} status {capture.get('status')}")

    replay = client.capture_authorization(authorization["id"], request_id=request_id)
    check(replay.get("id") == capture["id"], "replaying the capture with the same PayPal-Request-Id returns the same capture")

    confirmed = client.get_capture(capture["id"])
    check(confirmed.get("status") in ("COMPLETED", "PENDING"), f"server-side capture status {confirmed.get('status')}")
    check(confirmed["amount"]["value"] == amount, f"captured amount {confirmed['amount']['value']} {currency}")
    if confirmed.get("status") == "PENDING":
        print(f"  Note: capture is PENDING ({confirmed.get('status_details')}); this can happen in the sandbox.")


def void_flow(client: PayPalClient, amount: str, currency: str) -> None:
    print("\n== Flow 2: authorize -> void ==")
    authorization = create_and_authorize(client, amount, currency, "void")
    client.void_authorization(authorization["id"], request_id=new_request_id())
    confirmed = client.get_authorization(authorization["id"])
    check(confirmed.get("status") == "VOIDED", f"server-side authorization status {confirmed.get('status')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flow", choices=["capture", "void", "both"], default="both")
    parser.add_argument("--amount", default="20.00")
    parser.add_argument("--currency", default="USD")
    args = parser.parse_args()

    load_dotenv()
    try:
        amount = format_amount(args.amount)
        client = PayPalClient.from_env()
        print("  Getting an access token...")
        client._access_token()
        print("  [PASS] OAuth token received")
        if args.flow in ("capture", "both"):
            capture_flow(client, amount, args.currency)
        if args.flow in ("void", "both"):
            void_flow(client, amount, args.currency)
    except CheckFailed:
        print("\nRESULT: FAILED (see the FAIL line above)")
        return 1
    except (PayPalError, ValueError) as exc:
        print(f"\nRESULT: ERROR - {exc}")
        return 1
    print("\nRESULT: ALL CHECKS PASSED - the sandbox supports the ProofPay payment flow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
