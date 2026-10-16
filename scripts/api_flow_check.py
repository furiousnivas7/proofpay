"""Phase 1 check: drive the RUNNING ProofPay server through the full flow with the real PayPal sandbox.

Flow 1: create milestone -> agree requirements -> pay (you approve in PayPal) -> submit -> report -> capture
Flow 2: create milestone -> agree requirements -> pay (you approve in PayPal) -> void

Usage (in a second terminal, while `uvicorn proofpay.main:app` is running):
    python scripts/api_flow_check.py
    python scripts/api_flow_check.py --flow void
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class CheckFailed(Exception):
    pass


def check(condition: bool, label: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise CheckFailed(label)


class Api:
    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url, timeout=60.0)
        self.client = ("client", os.environ.get("DEMO_CLIENT_PASSWORD", "client-demo"))
        self.freelancer = ("freelancer", os.environ.get("DEMO_FREELANCER_PASSWORD", "freelancer-demo"))

    def call(self, method: str, path: str, auth, **kwargs) -> dict:
        response = self.http.request(method, path, auth=auth, **kwargs)
        if response.status_code >= 400:
            raise CheckFailed(f"{method} {path} returned {response.status_code}: {response.text}")
        return response.json()


def paid_milestone(api: Api, label: str, amount: str) -> dict:
    m = api.call("POST", "/milestones", api.client,
                 json={"title": f"API flow check ({label})", "brief": "Sandbox test", "amount": amount})
    check(m["status"] == "DRAFT", f"milestone #{m['id']} created as DRAFT")
    mid = m["id"]
    api.call("PUT", f"/milestones/{mid}/criteria", api.client, json={"criteria": ["README exists", "Tests pass"]})
    api.call("POST", f"/milestones/{mid}/criteria/approve", api.client)
    m = api.call("POST", f"/milestones/{mid}/criteria/approve", api.freelancer)
    check(m["status"] == "CRITERIA_AGREED", "both sides agreed the requirements")

    order = api.call("POST", f"/milestones/{mid}/payment/order", api.client)
    check(bool(order.get("approval_url")), f"PayPal order {order['order_id']} created")
    print("\n  Open this link, log in with your SANDBOX PERSONAL (buyer) account and approve:")
    print(f"  {order['approval_url']}")
    print("  PayPal then sends you back to ProofPay, which places the hold.")
    input("  Press Enter once you see 'Payment authorized'... ")

    for _ in range(10):
        m = api.call("GET", f"/milestones/{mid}", api.client)
        if m["status"] != "CRITERIA_AGREED":
            break
        time.sleep(1)
    if m["status"] == "CRITERIA_AGREED":  # the return page did not run; authorize directly
        m = api.call("POST", f"/milestones/{mid}/payment/authorize", api.client)
    check(m["status"] == "AUTHORIZED", f"PayPal hold placed (authorization {m['paypal']['authorization_id']})")
    print(f"  Hold expires: {m['expiry']['expires_at']}")
    return m


def capture_flow(api: Api, amount: str) -> None:
    print("\n== Flow 1: milestone -> hold -> submit -> report -> capture ==")
    m = paid_milestone(api, "capture", amount)
    mid = m["id"]
    m = api.call("POST", f"/milestones/{mid}/submissions", api.freelancer,
                 json={"repo_url": "https://github.com/furiousnivas7/proofpay"})
    check(m["status"] == "SUBMITTED", "freelancer submitted work")
    m = api.call("POST", f"/milestones/{mid}/verify", api.freelancer)
    check(m["status"] == "VERIFIED" and m["latest_report"] is not None, "requirement report created")
    m = api.call("POST", f"/milestones/{mid}/payment/capture", api.client)
    check(m["status"] == "CAPTURED", f"client captured; PayPal capture {m['paypal']['capture_id']} is {m['paypal']['capture_status']}")
    timeline = api.call("GET", f"/milestones/{mid}/timeline", api.client)
    calls = [e for e in timeline if e["kind"] == "paypal_call"]
    check(all(c["ok"] for c in calls), f"all {len(calls)} PayPal calls succeeded")


def void_flow(api: Api, amount: str) -> None:
    print("\n== Flow 2: milestone -> hold -> void ==")
    m = paid_milestone(api, "void", amount)
    m = api.call("POST", f"/milestones/{m['id']}/payment/void", api.client)
    check(m["status"] == "VOIDED", "client voided the hold and PayPal confirmed it")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flow", choices=["capture", "void", "both"], default="both")
    parser.add_argument("--amount", default="20.00")
    parser.add_argument("--base-url", default=os.environ.get("APP_BASE_URL", "http://localhost:8000"))
    args = parser.parse_args()

    api = Api(args.base_url)
    try:
        api.http.get("/health").raise_for_status()
        print(f"  [PASS] ProofPay server is running at {args.base_url}")
        if args.flow in ("capture", "both"):
            capture_flow(api, args.amount)
        if args.flow in ("void", "both"):
            void_flow(api, args.amount)
    except httpx.HTTPError as exc:
        print(f"\nRESULT: ERROR - cannot reach the server ({exc}). Is uvicorn running?")
        return 1
    except CheckFailed as exc:
        print(f"\nRESULT: FAILED - {exc}")
        return 1
    print("\nRESULT: ALL CHECKS PASSED - the full milestone flow works through the API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
