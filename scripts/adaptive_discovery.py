#!/usr/bin/env python3
"""Bounded state machine for known-company public discovery.

The state machine is provider-agnostic: the MANUAL_AGENT performs the actual
search/page opens, while this module defines the states, transitions, and hard
budgets that the runner and receipt validator can enforce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import monotonic
from typing import Any


class AdaptiveState(str, Enum):
    START = "START"
    COMPANY_HOME_FOUND = "COMPANY_HOME_FOUND"
    CAREER_ENTRY_FOUND = "CAREER_ENTRY_FOUND"
    CAMPUS_CAMPAIGN_FOUND = "CAMPUS_CAMPAIGN_FOUND"
    ATS_LIST_FOUND = "ATS_LIST_FOUND"
    ROLE_FOUND = "ROLE_FOUND"
    NO_RESULT_CONFIRMED = "NO_RESULT_CONFIRMED"
    BLOCKED = "BLOCKED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


TERMINAL_STATES = frozenset(
    {
        AdaptiveState.ROLE_FOUND.value,
        AdaptiveState.NO_RESULT_CONFIRMED.value,
        AdaptiveState.BLOCKED.value,
        AdaptiveState.NEEDS_REVIEW.value,
    }
)


@dataclass
class AdaptiveBudget:
    max_searches: int = 3
    max_opens: int = 8
    max_hops: int = 3
    max_seconds: int = 300
    searches: int = 0
    opens: int = 0
    hops: int = 0
    started_at: float = field(default_factory=monotonic)

    def elapsed_seconds(self) -> int:
        return max(0, int(monotonic() - self.started_at))

    def exhausted(self) -> bool:
        return (
            self.searches >= self.max_searches
            or self.opens >= self.max_opens
            or self.hops >= self.max_hops
            or self.elapsed_seconds() >= self.max_seconds
        )

    def consume(self, *, searches: int = 0, opens: int = 0, hops: int = 0) -> None:
        self.searches += max(0, int(searches))
        self.opens += max(0, int(opens))
        self.hops += max(0, int(hops))


@dataclass
class AdaptiveSearch:
    """Track one known-company query and reject invalid transitions."""

    budget: AdaptiveBudget = field(default_factory=AdaptiveBudget)
    state: str = AdaptiveState.START.value
    transitions: list[dict[str, Any]] = field(default_factory=list)
    page_types: list[str] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)

    def transition(self, state: str, *, reason: str = "") -> str:
        target = str(state or "").upper()
        allowed = {item.value for item in AdaptiveState}
        if target not in allowed:
            raise ValueError(f"unknown adaptive discovery state: {state}")
        if self.state in TERMINAL_STATES and target != self.state:
            raise ValueError(f"terminal state {self.state} cannot transition to {target}")
        self.state = target
        self.transitions.append({"state": target, "reason": str(reason or "")[:300]})
        return self.state

    def record_search(self) -> None:
        self.budget.consume(searches=1)
        if self.budget.searches > self.budget.max_searches:
            self.transition(AdaptiveState.BLOCKED.value, reason="search budget exhausted")

    def record_pages(self, urls: list[str], *, page_type: str = "UNKNOWN") -> None:
        unique = [str(url) for url in urls if str(url) and str(url) not in self.seen_urls]
        self.seen_urls.update(unique)
        self.budget.consume(opens=len(unique), hops=1 if unique else 0)
        if page_type:
            self.page_types.append(str(page_type))
        if self.budget.opens > self.budget.max_opens or self.budget.hops > self.budget.max_hops:
            self.transition(AdaptiveState.BLOCKED.value, reason="page/hop budget exhausted")

    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def receipt_fields(self) -> dict[str, Any]:
        return {
            "terminal_state": self.state if self.terminal() else AdaptiveState.NEEDS_REVIEW.value,
            "page_types": list(dict.fromkeys(self.page_types)),
            "search_calls": self.budget.searches,
            "open_calls": self.budget.opens,
            "hop_count": self.budget.hops,
            "run_seconds": min(self.budget.elapsed_seconds(), self.budget.max_seconds),
            "transition_log": list(self.transitions),
        }


def adaptive_budget_metadata() -> dict[str, int]:
    return {"max_searches": 3, "max_opens": 8, "max_hops": 3, "max_seconds": 300}

