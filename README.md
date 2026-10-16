# ProofPay

ProofPay is a project exploring a clearer way for freelancers and clients to manage milestone payments.

Clients set the work requirements for each milestone. When the freelancer submits their work, AI helps compare it with those requirements and explains what appears complete or missing. The client reviews the assessment and decides whether to approve the payment.

## How it’s intended to work

1. The client creates a project milestone with requirements and a payment amount.
2. The freelancer submits their work.
3. AI reviews the submission against the agreed requirements.
4. The client reviews the results and decides what happens next.
5. PayPal Sandbox is used to demonstrate the payment flow.

## About the payment flow

This project uses PayPal Sandbox for testing. It is a demonstration of milestone-based payments, not an escrow service. AI provides an assessment; the client makes the final decision.

## Project status

ProofPay is in development. Implemented so far: a sandbox-only PayPal client and a day-1 check that the sandbox supports the payment flow.

## Day-1 PayPal Sandbox check

`scripts/sandbox_check.py` confirms your PayPal Sandbox app can do the flow ProofPay depends on:

1. **Authorize → capture:** create an order, approve it as the sandbox buyer, authorize, capture, replay the capture with the same `PayPal-Request-Id` to confirm idempotency, and verify the capture server-side.
2. **Authorize → void:** create and authorize a second order, void it, and verify it is `VOIDED` server-side.

### Setup

1. In the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/), create a **Sandbox** REST app and note its client ID and secret. Under *Testing Tools → Sandbox Accounts*, note the login of the default **Personal** (buyer) account.
2. Install and configure:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements-dev.txt
   cp .env.example .env   # then add your sandbox client ID and secret
   ```

3. Run the check and follow the prompts (you approve each order in the browser with the sandbox buyer account):

   ```bash
   python scripts/sandbox_check.py               # both flows
   python scripts/sandbox_check.py --flow void   # one flow
   ```

4. Run the unit tests (no network or credentials needed): `pytest`

The client refuses any host other than `api-m.sandbox.paypal.com`, and credentials are read from `.env`, which git ignores.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
