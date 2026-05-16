"""Resilient LLM client wrapping multiple providers behind a single call.

Built for the DevNetwork "Resilient Agents" hackathon track.

Layers (outermost first):
    fallback_chain  → try targets in order, switch on failure or breaker open
    circuit_breaker → after N consecutive failures, freeze a target for cooldown_s
    retry           → exponential backoff per target on transient errors

Scorecard records every attempt so the demo can show recovery quantitatively.

Designed to be framework-agnostic; wraps any OpenAI-compatible endpoint
(TFY Gateway, raw OpenAI, Groq, Together, Mistral, ...). Each target is just
a (base_url, api_key, model_name) tuple.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError

log = logging.getLogger("resilient_llm")


# --------------------------------------------------------------------------- #
# Target config                                                               #
# --------------------------------------------------------------------------- #

@dataclass
class Target:
    """One backend behind the fallback chain. Order in the chain = priority."""
    name: str                       # human label e.g. "groq-llama3-8b"
    base_url: str
    api_key: str
    model: str                      # provider's model id
    # per-target retry + breaker tuning
    max_retries: int = 2
    base_delay_s: float = 0.4
    breaker_threshold: int = 3      # consecutive failures before opening
    breaker_cooldown_s: float = 30.0


# Status codes that should NOT be retried (auth / bad request etc.).
NON_RETRYABLE_STATUS = {400, 401, 403, 404, 422}


# --------------------------------------------------------------------------- #
# Scorecard                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class AttemptRecord:
    target: str
    model: str
    attempt: int                    # 1-indexed retry count within this target
    ok: bool
    latency_ms: float
    err_type: Optional[str] = None
    err_msg: Optional[str] = None


@dataclass
class CallRecord:
    """Everything that happened for one user-facing call. The headline metric
    `user_latency_ms` is wall-clock between submit and successful response."""
    ok: bool
    user_latency_ms: float
    final_target: Optional[str]
    final_model: Optional[str]
    attempts: list[AttemptRecord] = field(default_factory=list)
    fallback_jumps: int = 0         # how many *targets* (not retries) we tried


@dataclass
class Scorecard:
    """Aggregate stats across all calls in a run. The demo prints this at
    the end to give judges a quantitative resilience signal."""
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.calls)

    @property
    def successes(self) -> int:
        return sum(1 for c in self.calls if c.ok)

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total else 0.0

    @property
    def avg_user_latency_ms(self) -> float:
        ok = [c.user_latency_ms for c in self.calls if c.ok]
        return sum(ok) / len(ok) if ok else 0.0

    @property
    def fallback_trigger_rate(self) -> float:
        triggered = sum(1 for c in self.calls if c.fallback_jumps > 0)
        return triggered / self.total if self.total else 0.0

    @property
    def avg_recovery_attempts(self) -> float:
        rec = [len(c.attempts) for c in self.calls if c.ok and c.fallback_jumps > 0]
        return sum(rec) / len(rec) if rec else 0.0

    def _pct(self, vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        vs = sorted(vals)
        k = max(0, min(len(vs) - 1, int(round(q * (len(vs) - 1)))))
        return vs[k]

    @property
    def p50_user_latency_ms(self) -> float:
        return self._pct([c.user_latency_ms for c in self.calls if c.ok], 0.50)

    @property
    def p95_user_latency_ms(self) -> float:
        return self._pct([c.user_latency_ms for c in self.calls if c.ok], 0.95)

    @property
    def avg_mttr_ms(self) -> float:
        """Mean Time To Recovery — for calls that *did* hit a failure, how long
        from first attempt to the successful one. 0 if no recovered calls."""
        recovery_times: list[float] = []
        for c in self.calls:
            if c.ok and any(not a.ok for a in c.attempts):
                t = sum(a.latency_ms for a in c.attempts)
                recovery_times.append(t)
        return sum(recovery_times) / len(recovery_times) if recovery_times else 0.0

    @property
    def by_target(self) -> dict[str, dict[str, float]]:
        """Per-target final-resolver counts and ok-rate."""
        out: dict[str, dict[str, float]] = {}
        for c in self.calls:
            if c.final_target is None:
                continue
            slot = out.setdefault(c.final_target, {"served": 0, "total_attempts": 0})
            slot["served"] += 1
        for c in self.calls:
            for a in c.attempts:
                slot = out.setdefault(a.target, {"served": 0, "total_attempts": 0})
                slot["total_attempts"] += 1
        return out

    def render(self) -> str:
        by = self.by_target
        target_lines = "\n".join(
            f"    {name:<22} served={int(v['served']):>3}  attempts={int(v['total_attempts']):>3}"
            for name, v in sorted(by.items(), key=lambda kv: -kv[1]["served"])
        )
        return (
            "─── Resilience Scorecard ───\n"
            f"  total calls          : {self.total}\n"
            f"  success rate         : {self.success_rate * 100:5.1f}%\n"
            f"  user latency p50/p95 : {self.p50_user_latency_ms:6.0f} / {self.p95_user_latency_ms:6.0f} ms\n"
            f"  avg user latency     : {self.avg_user_latency_ms:7.0f} ms\n"
            f"  fallback trigger rate: {self.fallback_trigger_rate * 100:5.1f}%\n"
            f"  MTTR (recovered calls): {self.avg_mttr_ms:6.0f} ms\n"
            f"  by target:\n{target_lines}\n"
        )


# --------------------------------------------------------------------------- #
# Circuit breaker                                                             #
# --------------------------------------------------------------------------- #

class _Breaker:
    """One breaker per target. Trips OPEN after N consecutive failures and
    refuses traffic for `cooldown_s`, then HALF_OPEN lets one probe through."""

    def __init__(self, threshold: int, cooldown_s: float):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.fail_count = 0
        self.opened_at: Optional[float] = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_s:
            return True  # half-open probe
        return False

    def record_success(self) -> None:
        self.fail_count = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.threshold:
            self.opened_at = time.monotonic()


# --------------------------------------------------------------------------- #
# Resilient client                                                            #
# --------------------------------------------------------------------------- #

# Pluggable fault injector. Tests / chaos.py replace this to force failures.
FaultHook = Callable[[Target, int], None]


class ResilientLLM:
    """Multi-provider chat completion with retry → breaker → fallback chain."""

    def __init__(
        self,
        targets: list[Target],
        scorecard: Optional[Scorecard] = None,
        fault_hook: Optional[FaultHook] = None,
    ):
        if not targets:
            raise ValueError("need at least one target")
        self.targets = targets
        self.scorecard = scorecard or Scorecard()
        self.fault_hook = fault_hook
        # max_retries=0 turns OFF the OpenAI SDK's built-in retry layer so our
        # ResilientLLM is the *only* control plane. Otherwise SDK retries + our
        # retries stack: one request becomes 2 (SDK) × (max_retries+1) (ours)
        # attempts and the latency in demo.py wouldn't reflect what an operator
        # would see in production.
        self._clients: dict[str, OpenAI] = {
            t.name: OpenAI(base_url=t.base_url, api_key=t.api_key, max_retries=0)
            for t in targets
        }
        self._breakers: dict[str, _Breaker] = {
            t.name: _Breaker(t.breaker_threshold, t.breaker_cooldown_s) for t in targets
        }

    def chat(self, messages: list[dict], **openai_kwargs: Any) -> tuple[Any, CallRecord]:
        """Send chat messages. Returns (openai_response, CallRecord)."""
        call_start = time.monotonic()
        rec = CallRecord(ok=False, user_latency_ms=0.0, final_target=None, final_model=None)

        for tgt_idx, tgt in enumerate(self.targets):
            breaker = self._breakers[tgt.name]
            if not breaker.allow():
                log.info("[%s] breaker OPEN, skipping", tgt.name)
                continue

            for attempt in range(1, tgt.max_retries + 2):  # +1 for initial try
                t0 = time.monotonic()
                err_type: Optional[str] = None
                err_msg: Optional[str] = None
                response: Any = None

                try:
                    # Chaos hook lets tests force a failure before the real call.
                    if self.fault_hook is not None:
                        self.fault_hook(tgt, attempt)
                    response = self._clients[tgt.name].chat.completions.create(
                        model=tgt.model, messages=messages, **openai_kwargs
                    )
                except (APIConnectionError, APITimeoutError) as e:
                    err_type, err_msg = type(e).__name__, str(e)[:160]
                except RateLimitError as e:
                    err_type, err_msg = "RateLimitError", str(e)[:160]
                except APIError as e:
                    err_type, err_msg = f"APIError({getattr(e, 'status_code', '?')})", str(e)[:160]
                    if getattr(e, "status_code", None) in NON_RETRYABLE_STATUS:
                        # Hard failure on this target — record and jump to next.
                        latency_ms = (time.monotonic() - t0) * 1000
                        rec.attempts.append(AttemptRecord(tgt.name, tgt.model, attempt, False, latency_ms, err_type, err_msg))
                        breaker.record_failure()
                        break
                except Exception as e:
                    err_type, err_msg = type(e).__name__, str(e)[:160]

                latency_ms = (time.monotonic() - t0) * 1000
                ok = err_type is None
                rec.attempts.append(AttemptRecord(tgt.name, tgt.model, attempt, ok, latency_ms, err_type, err_msg))

                if ok:
                    breaker.record_success()
                    rec.ok = True
                    rec.final_target = tgt.name
                    rec.final_model = tgt.model
                    rec.fallback_jumps = tgt_idx
                    rec.user_latency_ms = (time.monotonic() - call_start) * 1000
                    self.scorecard.calls.append(rec)
                    return response, rec

                breaker.record_failure()
                if attempt <= tgt.max_retries:
                    sleep_s = tgt.base_delay_s * (2 ** (attempt - 1)) + random.uniform(0, 0.1)
                    log.info("[%s] try %d failed (%s); retry in %.2fs", tgt.name, attempt, err_type, sleep_s)
                    time.sleep(sleep_s)

            # exhausted this target — fall through to next in chain

        rec.user_latency_ms = (time.monotonic() - call_start) * 1000
        rec.fallback_jumps = len(self.targets) - 1
        self.scorecard.calls.append(rec)
        return None, rec
