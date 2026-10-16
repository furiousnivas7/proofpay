from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proofpay.config import Settings
from proofpay.db import Base, make_engine, make_session_factory
from proofpay.main import create_app
from tests.fakes import FakePayPal

CLIENT = ("client", "client-demo")
FREELANCER = ("freelancer", "freelancer-demo")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        paypal_client_id="test-id",
        paypal_client_secret="test-secret",
        paypal_webhook_id="WH-TEST",
        app_base_url="http://testserver",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        demo_client_password=CLIENT[1],
        demo_freelancer_password=FREELANCER[1],
    )


@pytest.fixture
def session_factory(settings):
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def api(settings, paypal, session_factory):
    app = create_app(settings=settings, paypal=paypal, session_factory=session_factory)
    with TestClient(app) as client:
        yield client


class Flow:
    """Drives a milestone through the API, one step per method."""

    def __init__(self, api: TestClient, paypal: FakePayPal):
        self.api = api
        self.paypal = paypal
        self.id: int | None = None

    def ok(self, response, status=200):
        assert response.status_code == status, response.text
        return response.json()

    def get(self, as_user=CLIENT):
        return self.ok(self.api.get(f"/milestones/{self.id}", auth=as_user))

    def create(self, amount="200.00"):
        body = self.ok(
            self.api.post("/milestones", json={"title": "Bakery site", "brief": "One page", "amount": amount}, auth=CLIENT),
            201,
        )
        self.id = body["id"]
        return body

    def agree_criteria(self):
        self.ok(self.api.put(f"/milestones/{self.id}/criteria", json={"criteria": ["Shows menu", "Has contact form"]}, auth=CLIENT))
        self.ok(self.api.post(f"/milestones/{self.id}/criteria/approve", auth=CLIENT))
        return self.ok(self.api.post(f"/milestones/{self.id}/criteria/approve", auth=FREELANCER))

    def authorize(self):
        order = self.ok(self.api.post(f"/milestones/{self.id}/payment/order", auth=CLIENT))
        self.paypal.approve(order["order_id"])
        page = self.api.get(f"/paypal/return?milestone_id={self.id}&token={order['order_id']}")
        assert page.status_code == 200, page.text
        return self.get()

    def submit(self):
        return self.ok(
            self.api.post(f"/milestones/{self.id}/submissions", json={"repo_url": "https://github.com/x/y"}, auth=FREELANCER),
            201,
        )

    def verify(self):
        return self.ok(self.api.post(f"/milestones/{self.id}/verify", auth=FREELANCER))

    def to(self, status: str):
        steps = ["DRAFT", "CRITERIA_AGREED", "AUTHORIZED", "SUBMITTED", "VERIFIED"]
        actions = [self.create, self.agree_criteria, self.authorize, self.submit, self.verify]
        for step, action in zip(steps, actions):
            action()
            if step == status:
                return self.get()
        raise ValueError(status)


@pytest.fixture
def flow(api, paypal):
    return Flow(api, paypal)
