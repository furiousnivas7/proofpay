"""Every allowed and every blocked status change, checked against a table written out independently."""

import itertools

import pytest

from proofpay.states import InvalidTransition, MilestoneStatus as S, can_transition, ensure_transition

EXPECTED_ALLOWED = {
    (S.DRAFT, S.CRITERIA_AGREED),
    (S.CRITERIA_AGREED, S.AUTHORIZED),
    (S.AUTHORIZED, S.SUBMITTED),
    (S.AUTHORIZED, S.VOIDED),
    (S.AUTHORIZED, S.EXPIRED),
    (S.SUBMITTED, S.VERIFIED),
    (S.SUBMITTED, S.VOIDED),
    (S.SUBMITTED, S.EXPIRED),
    (S.VERIFIED, S.CAPTURED),
    (S.VERIFIED, S.REVISION_REQUESTED),
    (S.VERIFIED, S.VOIDED),
    (S.VERIFIED, S.EXPIRED),
    (S.REVISION_REQUESTED, S.SUBMITTED),
    (S.REVISION_REQUESTED, S.VOIDED),
    (S.REVISION_REQUESTED, S.EXPIRED),
}

ALL_PAIRS = list(itertools.product(S, S))


@pytest.mark.parametrize("current,target", sorted(EXPECTED_ALLOWED, key=str))
def test_allowed(current, target):
    assert can_transition(current, target)
    ensure_transition(current, target)


@pytest.mark.parametrize("current,target", [p for p in ALL_PAIRS if p not in EXPECTED_ALLOWED])
def test_blocked(current, target):
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransition):
        ensure_transition(current, target)


@pytest.mark.parametrize("final", [S.CAPTURED, S.VOIDED, S.EXPIRED])
def test_final_statuses_have_no_way_out(final):
    assert not any(can_transition(final, target) for target in S)
