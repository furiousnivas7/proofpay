# Testing ProofPay

This guide walks you through every test you need to run, in order. Each part says what to run, what you should see, and what to do if it fails.

| Part | What it proves | Plan step | Time |
|---|---|---|---|
| 1. Setup | Your machine can run ProofPay | — | 5 min |
| 2. Unit tests | The code behaves correctly without touching PayPal | 1.3 | 1 min |
| 3. Sandbox check | Your PayPal sandbox app supports authorize, capture and void | 0.4 | 10 min |
| 4. Full API flow | The ProofPay server runs the whole milestone flow with real sandbox PayPal | 1.4, 1.6, 1.7 | 10 min |
| 5. Click through by hand | You can drive each step yourself in the browser | — | 15 min |
| 6. Webhooks | PayPal can notify ProofPay, and ProofPay verifies and de-duplicates it | 1.5 | 20 min |

---

## 1. Setup (once)

You need Python 3.10 or newer.

```bash
cd ~/Documents/GitHub/proofpay
bash scripts/setup.sh
```

The script creates `.venv`, installs packages, creates `.env` if it is missing, builds the database and runs the unit tests.

Then open `.env` and check it has your **sandbox** credentials:

```
PAYPAL_CLIENT_ID=<your sandbox client ID>
PAYPAL_CLIENT_SECRET=<your sandbox secret>
PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com
```

> **Secrets go in `.env` only.** `.env` is ignored by git. `.env.example` is committed, so it must always keep empty values.

Every new terminal needs the virtual environment switched on first:

```bash
source .venv/bin/activate
```

---

## 2. Unit tests

```bash
pytest
```

**You should see:** `127 passed`. No network or PayPal account is used. A fake PayPal stands in for the real one.

What they cover:

- Every allowed and every blocked milestone status change.
- The full flow through the API: happy path, void, revision and resubmit.
- A capture whose response is lost, then retried, captures only once.
- The client and freelancer can each only do their own actions.
- Webhooks with a bad signature are rejected, and duplicates are ignored.
- Expiry warnings and automatic expiry.
- The database migration builds exactly the tables the code expects.

**If something fails:** send me the full `pytest` output.

---

## 3. PayPal sandbox check (plan step 0.4)

This talks to the real PayPal sandbox. It uses fake money.

### Find your sandbox buyer login

1. Go to [developer.paypal.com/dashboard](https://developer.paypal.com/dashboard/) and make sure the toggle at the top says **Sandbox**.
2. Open **Testing Tools → Sandbox Accounts**.
3. Find the account whose type is **Personal**. Click the **⋮** menu, then **View/Edit account**.
4. Note its **email** and **password**. This is the "buyer" who approves payments. It is not your real PayPal login.

### Run it

```bash
python scripts/sandbox_check.py
```

The script pauses twice and prints a link each time. For each pause:

1. Open the link in a **private/incognito window**, so your real PayPal login doesn't interfere.
2. Log in with the sandbox **Personal** email and password.
3. Click the button to complete the purchase.
4. You land on an `example.com` page. That is expected.
5. Go back to the terminal and press **Enter**.

**You should see** every line marked `[PASS]` and this at the end:

```
RESULT: ALL CHECKS PASSED - the sandbox supports the ProofPay payment flow.
```

**If it fails**, find the message below:

| Message contains | Cause | Fix |
|---|---|---|
| `401` or `invalid_client` | Wrong client ID or secret, or live credentials | Copy them again from the **Sandbox** app |
| `order status is APPROVED (got PAYER_ACTION_REQUIRED)` | Enter was pressed before approving | Run it again and approve first |
| `Only the PayPal sandbox is allowed` | `PAYPAL_BASE_URL` is wrong | Set it to `https://api-m.sandbox.paypal.com` |
| The PayPal login page rejects you | You used your real PayPal account | Use the sandbox **Personal** account |

Send me the whole terminal output either way.

---

## 4. Full API flow with real sandbox PayPal (Phase 1)

This runs the actual ProofPay server and drives a milestone from draft to captured, then a second one from draft to voided.

**Terminal 1**, start the server and leave it running:

```bash
source .venv/bin/activate
uvicorn proofpay.main:app --reload
```

**Terminal 2**, run the check:

```bash
source .venv/bin/activate
python scripts/api_flow_check.py
```

It pauses twice for you to approve in PayPal, the same way as Part 3. This time, after approving, PayPal sends you back to ProofPay at `localhost:8000`, and you should see a page saying **Payment authorized**. Then press Enter in terminal 2.

**You should see:**

```
RESULT: ALL CHECKS PASSED - the full milestone flow works through the API.
```

**If it fails:** send me the output of terminal 2 and the last 30 lines of terminal 1.

---

## 5. Click through by hand

Open [http://localhost:8000/docs](http://localhost:8000/docs) while the server runs. This page lists every endpoint and lets you call them.

**To log in**, click **Authorize** at the top right. Use username `client` and password `client-demo`, or `freelancer` and `freelancer-demo`. These come from `.env`. To switch user, click **Authorize**, then **Logout**, then log in as the other one.

For each step: open the endpoint, click **Try it out**, fill in the fields, then click **Execute**. Use the milestone `id` from step 1 everywhere.

| # | Log in as | Endpoint | What to enter | Status afterwards |
|---|---|---|---|---|
| 1 | client | `POST /milestones` | `{"title": "Bakery site", "brief": "One page site", "amount": "200.00"}` | `DRAFT` |
| 2 | client | `PUT /milestones/{id}/criteria` | `{"criteria": ["Shows the menu", "Has a contact form"]}` | `DRAFT` |
| 3 | client | `POST /milestones/{id}/criteria/approve` | — | `DRAFT` |
| 4 | freelancer | `POST /milestones/{id}/criteria/approve` | — | `CRITERIA_AGREED` |
| 5 | client | `POST /milestones/{id}/payment/order` | — | Open the `approval_url` and approve as the sandbox buyer |
| 6 | client | `GET /milestones/{id}` | — | `AUTHORIZED`, with `expiry` dates filled in |
| 7 | freelancer | `POST /milestones/{id}/submissions` | `{"repo_url": "https://github.com/furiousnivas7/proofpay"}` | `SUBMITTED` |
| 8 | freelancer | `POST /milestones/{id}/verify` | — | `VERIFIED`, with a `latest_report` |
| 9 | client | `POST /milestones/{id}/payment/capture` | — | `CAPTURED` |
| 10 | either | `GET /milestones/{id}/timeline` | — | Every status change and PayPal call, in order |

Things worth trying, which should all be **refused**:

- As **freelancer**, call `payment/capture`. Expect `403 Only the client can do this`.
- As **client**, call `payment/capture` while the status is `SUBMITTED`. Expect `409 Cannot move a milestone from SUBMITTED to CAPTURED`.
- Log in with a wrong password. Expect `401`.

The `your_actions` field in every milestone response lists what the logged-in user can do next.

The report in step 8 marks every requirement `unclear` for now. The AI checker replaces it in Phase 2.

You can also check a milestone in the sandbox itself: [sandbox.paypal.com](https://www.sandbox.paypal.com) → log in as the sandbox **Business** account → **Activity**.

---

## 6. Webhooks (plan step 1.5)

PayPal needs a public `https` address to send webhooks to. A free Cloudflare tunnel gives your laptop one.

1. Install the tunnel tool once:

   ```bash
   brew install cloudflared
   ```

2. With the server running in terminal 1, open **terminal 3**:

   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```

   It prints an address like `https://random-words.trycloudflare.com`. Keep this terminal open. The address changes every time you restart it.

3. In the PayPal Developer Dashboard, open **Apps & Credentials → your sandbox app → Sandbox Webhooks → Add Webhook**.
   - **Webhook URL:** `https://random-words.trycloudflare.com/webhooks/paypal`
   - **Events:** tick *Payment authorization created*, *Payment authorization voided*, *Payment capture completed*, *Payment capture denied* and *Payment capture pending*.
   - Save, then copy the **Webhook ID** it shows.

4. Put it in `.env`:

   ```
   PAYPAL_WEBHOOK_ID=<the webhook ID>
   ```

5. Stop the server with Ctrl+C and start it again so it reads the new value.

6. Run Part 4 again: `python scripts/api_flow_check.py`.

7. Check the events arrived and were verified:

   ```bash
   sqlite3 proofpay.db "select event_type, received_at from webhook_events"
   ```

   **You should see** rows such as `PAYMENT.AUTHORIZATION.CREATED` and `PAYMENT.CAPTURE.COMPLETED`. They can take a minute to arrive. Each milestone's timeline also shows a note for each event.

Only events that PayPal confirms as genuine are stored. The dashboard's **Webhooks Simulator** sends test events that PayPal cannot verify, so ProofPay correctly rejects them with `400`. Use the real flow from Part 4 instead.

If you change `APP_BASE_URL` to the tunnel address, PayPal's return page also goes through the tunnel. That is optional. Leaving it as `http://localhost:8000` works on your laptop.

---

## Start again with an empty database

```bash
rm proofpay.db
alembic upgrade head
```

## What to send me after testing

- The last lines of Part 3: `ALL CHECKS PASSED` or the error.
- The last lines of Part 4.
- The output of the `sqlite3` command in Part 6, if you did it.
- Anything that confused you or looked wrong.
