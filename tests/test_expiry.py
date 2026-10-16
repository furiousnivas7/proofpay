"""The 3-day honor period, the 29-day expiry and the warnings shown before a hold lapses."""

from datetime import timedelta

from proofpay.db import utcnow
from proofpay.models import Milestone
from proofpay.services import expiry_info
from proofpay.states import MilestoneStatus


def make(settings, status=MilestoneStatus.AUTHORIZED, days_ago=0):
    authorized = utcnow() - timedelta(days=days_ago)
    return Milestone(status=status, authorized_at=authorized, expires_at=authorized + timedelta(days=29))


def test_fresh_hold_has_no_warning(settings):
    info = expiry_info(make(settings), settings)
    assert info["warning"] is None
    assert info["expires_at"] - info["authorized_at"] == timedelta(days=29)
    assert info["honor_period_ends_at"] - info["authorized_at"] == timedelta(days=3)


def test_warns_after_honor_period(settings):
    assert "honor period" in expiry_info(make(settings, days_ago=4), settings)["warning"]


def test_warns_close_to_expiry(settings):
    assert "expires in" in expiry_info(make(settings, days_ago=27), settings)["warning"]


def test_no_warning_once_captured(settings):
    assert expiry_info(make(settings, MilestoneStatus.CAPTURED, days_ago=27), settings)["warning"] is None


def test_milestone_expires_when_hold_lapses(flow, session_factory):
    flow.to("AUTHORIZED")
    with session_factory() as session:
        milestone = session.get(Milestone, flow.id)
        milestone.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
    m = flow.get()
    assert m["status"] == "EXPIRED"
    assert m["your_actions"] == []
