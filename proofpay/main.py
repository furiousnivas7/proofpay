"""ProofPay API. Run with:  uvicorn proofpay.main:app --reload   then open http://localhost:8000/docs"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from html import escape
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import inspect, or_, select
from sqlalchemy.orm import Session, sessionmaker

from proofpay import services
from proofpay.auth import current_user, get_session, seed_demo_users
from proofpay.config import Settings, get_settings
from proofpay.db import make_engine, make_session_factory
from proofpay.models import Milestone, User
from proofpay.paypal_client import PayPalClient
from proofpay.services import ServiceError
from proofpay.states import InvalidTransition
from proofpay.webhooks import handle_webhook


# ---------------------------------------------------------------- request bodies


class MilestoneCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200, examples=["Landing page for a bakery"])
    brief: str = Field(default="", examples=["A one-page site with a menu, opening hours and a contact form."])
    amount: str = Field(examples=["200.00"])


class CriteriaUpdate(BaseModel):
    criteria: list[str] = Field(min_length=1, examples=[["Page shows the menu", "Contact form sends an email"]])


class SubmissionCreate(BaseModel):
    repo_url: str | None = Field(default=None, examples=["https://github.com/example/bakery-site"])
    content: str | None = None
    notes: str | None = None


class RevisionRequest(BaseModel):
    note: str = Field(min_length=1, examples=["The contact form does not send anything yet."])


# ---------------------------------------------------------------- app


def create_app(
    settings: Settings | None = None,
    paypal: PayPalClient | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        session = session_factory()
        try:
            if not inspect(session.get_bind()).has_table("users"):
                raise RuntimeError("Database tables are missing. Run:  alembic upgrade head")
            seed_demo_users(session)
        finally:
            session.close()
        yield

    app = FastAPI(
        title="ProofPay",
        description=(
            "Milestone payments where AI checks the work against agreed requirements and the client "
            "decides whether to release the PayPal hold. Sandbox only, and not an escrow service.\n\n"
            "Click **Authorize** and log in as `client` or `freelancer` (passwords are in your .env)."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.paypal = paypal

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(InvalidTransition)
    async def invalid_transition(_: Request, exc: InvalidTransition):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def get_paypal(request: Request) -> PayPalClient:
        if request.app.state.paypal is None:
            s = request.app.state.settings
            request.app.state.paypal = PayPalClient(s.paypal_client_id, s.paypal_client_secret, s.paypal_base_url)
        return request.app.state.paypal

    def load(session: Session, milestone_id: int, user: User) -> Milestone:
        milestone = services.get_milestone(session, milestone_id, user)
        services.refresh_expiry(session, milestone, settings)
        return milestone

    def view(milestone: Milestone, user: User) -> dict[str, Any]:
        latest = milestone.verifications[-1] if milestone.verifications else None
        return {
            "id": milestone.id,
            "title": milestone.title,
            "brief": milestone.brief,
            "amount": milestone.amount,
            "currency": milestone.currency,
            "status": milestone.status.value,
            "client": milestone.client.username,
            "freelancer": milestone.freelancer.username,
            "criteria": [{"id": c.id, "position": c.position, "text": c.text} for c in milestone.criteria],
            "criteria_approved_by": {
                "client": milestone.client_approved_criteria,
                "freelancer": milestone.freelancer_approved_criteria,
            },
            "paypal": {
                "order_id": milestone.paypal_order_id,
                "authorization_id": milestone.authorization_id,
                "authorization_status": milestone.authorization_status,
                "capture_id": milestone.capture_id,
                "capture_status": milestone.capture_status,
            },
            "expiry": services.expiry_info(milestone, settings),
            "submissions": [
                {"id": s.id, "repo_url": s.repo_url, "content": s.content, "notes": s.notes, "created_at": s.created_at}
                for s in milestone.submissions
            ],
            "latest_report": None
            if latest is None
            else {
                "id": latest.id,
                "submission_id": latest.submission_id,
                "verifier": latest.verifier,
                "summary": latest.summary,
                "results": latest.results,
                "created_at": latest.created_at,
            },
            "your_actions": services.allowed_actions(milestone, user),
        }

    # ------------------------------------------------------------ routes

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok"}

    @app.get("/me", tags=["system"])
    def me(user: User = Depends(current_user)):
        return {"username": user.username, "role": user.role}

    @app.post("/milestones", status_code=201, tags=["milestones"])
    def create_milestone(body: MilestoneCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
        freelancer = session.scalar(select(User).where(User.role == "freelancer"))
        milestone = services.create_milestone(session, user, freelancer, body.title, body.brief, body.amount, settings)
        return view(milestone, user)

    @app.get("/milestones", tags=["milestones"])
    def list_milestones(user: User = Depends(current_user), session: Session = Depends(get_session)):
        rows = session.scalars(
            select(Milestone)
            .where(or_(Milestone.client_id == user.id, Milestone.freelancer_id == user.id))
            .order_by(Milestone.id.desc())
        ).all()
        for milestone in rows:
            services.refresh_expiry(session, milestone, settings)
        return [
            {"id": m.id, "title": m.title, "amount": m.amount, "status": m.status.value,
             "your_actions": services.allowed_actions(m, user)}
            for m in rows
        ]

    @app.get("/milestones/{milestone_id}", tags=["milestones"])
    def get_milestone(milestone_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
        return view(load(session, milestone_id, user), user)

    @app.get("/milestones/{milestone_id}/timeline", tags=["milestones"])
    def timeline(milestone_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
        milestone = load(session, milestone_id, user)
        entries = [
            {"at": e.created_at, "kind": "status" if e.to_status else "note", "actor": e.actor,
             "from_status": e.from_status, "to_status": e.to_status, "message": e.message}
            for e in milestone.events
        ] + [
            {"at": c.created_at, "kind": "paypal_call", "operation": c.operation, "ok": c.ok,
             "resource_id": c.resource_id, "request_id": c.request_id, "http_status": c.http_status,
             "debug_id": c.debug_id, "error": c.error}
            for c in milestone.paypal_calls
        ]
        return sorted(entries, key=lambda e: e["at"])

    @app.put("/milestones/{milestone_id}/criteria", tags=["requirements"])
    def set_criteria(milestone_id: int, body: CriteriaUpdate, user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
        milestone = services.set_criteria(session, load(session, milestone_id, user), user, body.criteria)
        return view(milestone, user)

    @app.post("/milestones/{milestone_id}/criteria/approve", tags=["requirements"])
    def approve_criteria(milestone_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
        milestone = services.approve_criteria(session, load(session, milestone_id, user), user)
        return view(milestone, user)

    @app.post("/milestones/{milestone_id}/payment/order", tags=["payment"])
    def create_order(milestone_id: int, user: User = Depends(current_user), session: Session = Depends(get_session),
                     paypal: PayPalClient = Depends(get_paypal)):
        """Step 1 of paying: returns `approval_url`. Open it and log in as the sandbox buyer."""
        return services.create_order(session, load(session, milestone_id, user), user, paypal, settings)

    @app.post("/milestones/{milestone_id}/payment/authorize", tags=["payment"])
    def authorize(milestone_id: int, user: User = Depends(current_user), session: Session = Depends(get_session),
                  paypal: PayPalClient = Depends(get_paypal)):
        """Step 2 of paying: places the hold after approval. PayPal's return page also does this for you."""
        milestone = load(session, milestone_id, user)
        services.require_party(milestone, user, "client")
        return view(services.authorize(session, milestone, user.username, paypal, settings), user)

    @app.post("/milestones/{milestone_id}/submissions", status_code=201, tags=["work"])
    def submit(milestone_id: int, body: SubmissionCreate, user: User = Depends(current_user),
               session: Session = Depends(get_session)):
        milestone = load(session, milestone_id, user)
        services.submit_work(session, milestone, user, body.repo_url, body.content, body.notes)
        return view(milestone, user)

    @app.post("/milestones/{milestone_id}/verify", tags=["work"])
    def verify(milestone_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
        """Checks the latest submission against the requirements. Phase 1 uses a placeholder checker."""
        milestone = load(session, milestone_id, user)
        services.verify(session, milestone, user, request_verifier(app))
        return view(milestone, user)

    @app.post("/milestones/{milestone_id}/revision", tags=["work"])
    def request_revision(milestone_id: int, body: RevisionRequest, user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
        milestone = services.request_revision(session, load(session, milestone_id, user), user, body.note)
        return view(milestone, user)

    @app.post("/milestones/{milestone_id}/payment/capture", tags=["payment"])
    def capture(milestone_id: int, user: User = Depends(current_user), session: Session = Depends(get_session),
                paypal: PayPalClient = Depends(get_paypal)):
        """The client releases the payment. Only allowed once a requirement report exists."""
        return view(services.capture(session, load(session, milestone_id, user), user, paypal), user)

    @app.post("/milestones/{milestone_id}/payment/void", tags=["payment"])
    def void(milestone_id: int, user: User = Depends(current_user), session: Session = Depends(get_session),
             paypal: PayPalClient = Depends(get_paypal)):
        """The client cancels the hold and keeps their money."""
        return view(services.void(session, load(session, milestone_id, user), user, paypal), user)

    # ------------------------------------------------------------ PayPal redirects and webhooks

    @app.get("/paypal/return", response_class=HTMLResponse, include_in_schema=False)
    def paypal_return(milestone_id: int, token: str = "", session: Session = Depends(get_session),
                      paypal: PayPalClient = Depends(get_paypal)):
        milestone = session.get(Milestone, milestone_id)
        if milestone is None or not token or token != milestone.paypal_order_id:
            return _page("Unknown payment", "This PayPal return does not match a ProofPay milestone.", 404)
        try:
            services.authorize(session, milestone, "paypal-return", paypal, settings)
        except (ServiceError, InvalidTransition) as exc:
            return _page("Payment not authorized", str(getattr(exc, "message", exc)), 409)
        return _page(
            "Payment authorized",
            f"PayPal is now holding {milestone.amount} {milestone.currency} for milestone #{milestone.id}. "
            "Nothing has been charged yet. You can close this tab.",
        )

    @app.get("/paypal/cancel", response_class=HTMLResponse, include_in_schema=False)
    def paypal_cancel(milestone_id: int):
        return _page("Payment cancelled", f"You cancelled the PayPal approval for milestone #{milestone_id}.")

    @app.post("/webhooks/paypal", tags=["system"])
    async def paypal_webhook(request: Request, session: Session = Depends(get_session),
                             paypal: PayPalClient = Depends(get_paypal)):
        try:
            event = json.loads(await request.body())
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Body is not JSON"})
        headers = {k.lower(): v for k, v in request.headers.items()}
        return {"result": handle_webhook(session, paypal, settings, headers, event)}

    return app


def request_verifier(app: FastAPI):
    return getattr(app.state, "verifier", None)


def _page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    html = (
        f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title>"
        f"<body style='font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p></body>"
    )
    return HTMLResponse(html, status_code=status_code)


app = create_app()
