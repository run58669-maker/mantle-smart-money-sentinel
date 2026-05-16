"""Chaos injection for ResilientLLM — force failures to demo recovery.

Two patterns:
    BurstFault — fail the next N calls against a named target then recover
    RandomFault — fail each call with probability p
    BrownoutFault — first attempt always succeeds with high latency, retries are fast (simulates partial degradation)
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from openai import APIConnectionError, APIError

from resilient_llm import Target


class _FakeAPIError(APIError):
    """Constructible APIError without needing a real httpx request."""

    def __init__(self, message: str, status_code: int = 503):
        # bypass APIError's strict init by setting attrs directly
        Exception.__init__(self, message)
        self.message = message
        self.status_code = status_code
        self.request = None
        self.body = None
        self.response = None


@dataclass
class BurstFault:
    """Hard-fail the next `count` calls against `target_name`."""
    target_name: str
    count: int
    status_code: int = 503
    remaining: int = field(init=False)

    def __post_init__(self) -> None:
        self.remaining = self.count

    def __call__(self, tgt: Target, attempt: int) -> None:
        if tgt.name == self.target_name and self.remaining > 0:
            self.remaining -= 1
            raise _FakeAPIError(f"chaos burst on {tgt.name} ({self.remaining} left)", self.status_code)


@dataclass
class RandomFault:
    """Fail each call against any target in `targets` with probability `p`."""
    targets: set[str]
    p: float = 0.3
    seed: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def __call__(self, tgt: Target, attempt: int) -> None:
        if tgt.name in self.targets and self._rng.random() < self.p:
            raise APIConnectionError(request=None)  # type: ignore[arg-type]


@dataclass
class BrownoutFault:
    """Inject `latency_s` of fake latency on first attempt only — simulates a
    slow but technically reachable provider that retries quickly recover from."""
    target_name: str
    latency_s: float = 2.5

    def __call__(self, tgt: Target, attempt: int) -> None:
        if tgt.name == self.target_name and attempt == 1:
            time.sleep(self.latency_s)


def chain_faults(*hooks):
    """Compose multiple fault hooks — first one to raise wins."""
    def composed(tgt: Target, attempt: int) -> None:
        for h in hooks:
            h(tgt, attempt)
    return composed


# ── MCP-specific faults ───────────────────────────────────────────────────── #
# Same signature as the LLM hooks so they can be dropped into ResilientMCP.
# Using duck-typed `target.name` rather than the Target type so this file
# stays import-cycle-free.

@dataclass
class MCPToolFault:
    """Simulate an MCP server returning an error for the next `count` calls.

    This is the MCP-side counterpart to BurstFault: it raises before the
    real streamable-http call is made, so ResilientMCP's retry / breaker /
    fallback path is what answers — exactly the question the hackathon
    challenge asks: 'How does your agent behave when an MCP server starts
    erroring out?'"""
    target_name: str
    count: int
    error_message: str = "mcp tool unavailable"
    remaining: int = field(init=False)

    def __post_init__(self) -> None:
        self.remaining = self.count

    def __call__(self, tgt, attempt: int) -> None:
        if tgt.name == self.target_name and self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError(f"chaos: {self.error_message} ({self.remaining} left)")


@dataclass
class MCPTimeoutFault:
    """Inject `latency_s` of artificial latency on first attempt only —
    simulates a slow MCP server. ResilientMCP doesn't have a per-call timeout
    yet (TODO), so for now this just shows up as elevated p95 in the scorecard
    without flipping the breaker."""
    target_name: str
    latency_s: float = 2.0

    def __call__(self, tgt, attempt: int) -> None:
        if tgt.name == self.target_name and attempt == 1:
            time.sleep(self.latency_s)
