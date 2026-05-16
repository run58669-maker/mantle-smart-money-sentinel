"""Mantle JSON-RPC client with built-in fallback chain.

Reuses the same retry → breaker → fallback primitives as ResilientLLM —
applied to RPC endpoints instead of LLM endpoints. If the primary Mantle
RPC browns out, we fall back to a secondary, and eventually to a third.
This is the "MCP server starts erroring out" answer applied to RPC.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from resilient_llm import AttemptRecord, CallRecord, Scorecard, _Breaker

log = logging.getLogger("mantle_rpc")


@dataclass
class RPCTarget:
    name: str
    url: str
    max_retries: int = 2
    base_delay_s: float = 0.4
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 30.0
    timeout_s: float = 8.0


DEFAULT_TARGETS = [
    RPCTarget(name="mantle-public",  url="https://rpc.mantle.xyz"),
    RPCTarget(name="mantle-ankr",    url="https://rpc.ankr.com/mantle"),
    RPCTarget(name="mantle-publicnode", url="https://mantle-rpc.publicnode.com"),
]


class ResilientRPC:
    def __init__(self, targets: list[RPCTarget] = None, scorecard: Optional[Scorecard] = None):
        self.targets = targets or DEFAULT_TARGETS
        self.scorecard = scorecard or Scorecard()
        self._breakers = {t.name: _Breaker(t.breaker_threshold, t.breaker_cooldown_s) for t in self.targets}
        self._client = httpx.Client(timeout=httpx.Timeout(8.0))

    def close(self):
        self._client.close()

    def call(self, method: str, params: list) -> tuple[Any, CallRecord]:
        call_start = time.monotonic()
        rec = CallRecord(ok=False, user_latency_ms=0.0, final_target=None, final_model=method)

        for tgt_idx, tgt in enumerate(self.targets):
            breaker = self._breakers[tgt.name]
            if not breaker.allow():
                log.info("[%s] breaker OPEN, skipping", tgt.name)
                continue

            for attempt in range(1, tgt.max_retries + 2):
                t0 = time.monotonic()
                err_type = err_msg = None
                result = None
                try:
                    resp = self._client.post(
                        tgt.url,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                        timeout=tgt.timeout_s,
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    if "error" in body:
                        err_type, err_msg = "RPCError", str(body["error"])[:160]
                    else:
                        result = body.get("result")
                except httpx.HTTPStatusError as e:
                    err_type, err_msg = f"HTTP({e.response.status_code})", str(e)[:160]
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    err_type, err_msg = type(e).__name__, str(e)[:160]
                except Exception as e:
                    err_type, err_msg = type(e).__name__, str(e)[:160]

                latency_ms = (time.monotonic() - t0) * 1000
                ok = err_type is None
                rec.attempts.append(AttemptRecord(tgt.name, method, attempt, ok, latency_ms, err_type, err_msg))

                if ok:
                    breaker.record_success()
                    rec.ok = True
                    rec.final_target = tgt.name
                    rec.fallback_jumps = tgt_idx
                    rec.user_latency_ms = (time.monotonic() - call_start) * 1000
                    self.scorecard.calls.append(rec)
                    return result, rec

                breaker.record_failure()
                if attempt <= tgt.max_retries:
                    sleep_s = tgt.base_delay_s * (2 ** (attempt - 1)) + random.uniform(0, 0.1)
                    time.sleep(sleep_s)

        rec.user_latency_ms = (time.monotonic() - call_start) * 1000
        rec.fallback_jumps = len(self.targets) - 1
        self.scorecard.calls.append(rec)
        return None, rec

    def block_number(self) -> tuple[Optional[int], CallRecord]:
        raw, rec = self.call("eth_blockNumber", [])
        return (int(raw, 16) if raw else None), rec

    def balance_wei(self, address: str, block: str = "latest") -> tuple[Optional[int], CallRecord]:
        raw, rec = self.call("eth_getBalance", [address, block])
        return (int(raw, 16) if raw else None), rec

    def tx_count(self, address: str, block: str = "latest") -> tuple[Optional[int], CallRecord]:
        raw, rec = self.call("eth_getTransactionCount", [address, block])
        return (int(raw, 16) if raw else None), rec

    def logs(self, from_block: int, to_block: int, address: str | None = None,
             topics: list | None = None) -> tuple[Optional[list], CallRecord]:
        """eth_getLogs window. Caller is responsible for keeping the window
        narrow — public RPCs cap log windows at 1k-10k blocks."""
        params = [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }]
        if address:
            params[0]["address"] = address
        if topics:
            params[0]["topics"] = topics
        return self.call("eth_getLogs", params)

    def block_by_number(self, block_num: int, with_txs: bool = False) -> tuple[Optional[dict], CallRecord]:
        """Pull a single block, optionally with full tx objects."""
        return self.call("eth_getBlockByNumber", [hex(block_num), with_txs])


# === ERC-20 / WMNT helpers ================================================== #

WMNT_ADDRESS = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"  # canonical wrapped MNT

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def address_to_topic(address: str) -> str:
    """Pad a 20-byte address to a 32-byte topic."""
    addr = address.lower().removeprefix("0x")
    return "0x" + addr.rjust(64, "0")


def topic_to_address(topic: str) -> str:
    """Reverse: take the last 20 bytes from a 32-byte topic."""
    return "0x" + topic.lower().removeprefix("0x")[-40:]
