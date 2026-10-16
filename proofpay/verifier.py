"""Requirement checkers. Phase 2 adds the AI checker behind this same interface.

A verifier only produces a report. It never captures or voids a payment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


@dataclass
class Report:
    summary: str
    results: list[dict[str, Any]] = field(default_factory=list)


class Verifier(Protocol):
    name: str

    def check(self, criteria: Sequence[Any], submission: Any) -> Report: ...


class PlaceholderVerifier:
    """Marks every requirement "unclear" so the client reviews it by hand. Replaced in Phase 2."""

    name = "placeholder-verifier"

    def check(self, criteria: Sequence[Any], submission: Any) -> Report:
        results = [
            {
                "criterion_id": c.id,
                "criterion": c.text,
                "result": "unclear",
                "reason": "Automatic checking is not built yet. Review this requirement yourself.",
                "evidence": None,
            }
            for c in criteria
        ]
        return Report(summary=f"0 passed, 0 failed, {len(results)} need a manual review", results=results)
