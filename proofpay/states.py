"""The milestone status flow. Every status change goes through `ensure_transition`."""

from __future__ import annotations

from enum import Enum


class MilestoneStatus(str, Enum):
    DRAFT = "DRAFT"  # client is writing the requirements
    CRITERIA_AGREED = "CRITERIA_AGREED"  # both sides approved the requirements; client can pay
    AUTHORIZED = "AUTHORIZED"  # PayPal holds the client's funds
    SUBMITTED = "SUBMITTED"  # freelancer submitted work
    VERIFIED = "VERIFIED"  # a requirement report is ready for the client
    REVISION_REQUESTED = "REVISION_REQUESTED"  # client asked for changes; freelancer resubmits
    CAPTURED = "CAPTURED"  # client released the payment (final)
    VOIDED = "VOIDED"  # client cancelled the hold (final)
    EXPIRED = "EXPIRED"  # the PayPal authorization lapsed (final)


S = MilestoneStatus

# Statuses in which PayPal holds an authorization that can still be captured or voided.
HOLD_ACTIVE = frozenset({S.AUTHORIZED, S.SUBMITTED, S.VERIFIED, S.REVISION_REQUESTED})
FINAL = frozenset({S.CAPTURED, S.VOIDED, S.EXPIRED})

ALLOWED_TRANSITIONS: dict[MilestoneStatus, frozenset[MilestoneStatus]] = {
    S.DRAFT: frozenset({S.CRITERIA_AGREED}),
    S.CRITERIA_AGREED: frozenset({S.AUTHORIZED}),
    S.AUTHORIZED: frozenset({S.SUBMITTED, S.VOIDED, S.EXPIRED}),
    S.SUBMITTED: frozenset({S.VERIFIED, S.VOIDED, S.EXPIRED}),
    S.VERIFIED: frozenset({S.CAPTURED, S.REVISION_REQUESTED, S.VOIDED, S.EXPIRED}),
    S.REVISION_REQUESTED: frozenset({S.SUBMITTED, S.VOIDED, S.EXPIRED}),
    S.CAPTURED: frozenset(),
    S.VOIDED: frozenset(),
    S.EXPIRED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: MilestoneStatus, target: MilestoneStatus):
        self.current = current
        self.target = target
        super().__init__(f"Cannot move a milestone from {current.value} to {target.value}")


def can_transition(current: MilestoneStatus, target: MilestoneStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: MilestoneStatus, target: MilestoneStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
