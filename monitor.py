"""Poll loop + state diff + anomaly detector.

For each wallet in watchlist.json:
  1. fetch current (balance, tx_count) via ResilientRPC
  2. compare to last-known state (state.json on disk)
  3. emit AnomalyEvent objects when thresholds tripped
  4. persist new state

State is just a flat dict keyed by address; restart-safe.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from llm_interpret import AnomalyEvent
from mantle_rpc import ResilientRPC

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
WATCHLIST_FILE = ROOT / "watchlist.json"


@dataclass
class WalletSnapshot:
    address: str
    balance_mnt: float
    tx_count: int
    block: int
    fetched_at: float = field(default_factory=time.time)


def load_state() -> dict[str, dict]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_watchlist() -> dict:
    return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))


def snapshot_wallet(rpc: ResilientRPC, address: str) -> Optional[WalletSnapshot]:
    bal_wei, rec_bal = rpc.balance_wei(address)
    tx, rec_tx = rpc.tx_count(address)
    bn, _ = rpc.block_number()
    if bal_wei is None or tx is None or bn is None:
        return None
    return WalletSnapshot(
        address=address,
        balance_mnt=bal_wei / 1e18,
        tx_count=tx,
        block=bn,
    )


def detect_anomalies(
    wallet_cfg: dict,
    prev: dict,
    cur: WalletSnapshot,
    thresholds: dict,
) -> list[AnomalyEvent]:
    """Compare prev → cur and return AnomalyEvent list."""
    events: list[AnomalyEvent] = []
    if not prev:
        return events  # first-seen, nothing to compare yet

    prev_bal = prev.get("balance_mnt", 0.0)
    prev_tx = prev.get("tx_count", 0)
    delta_bal = cur.balance_mnt - prev_bal
    delta_tx = cur.tx_count - prev_tx
    pct = (delta_bal / prev_bal * 100) if prev_bal else 0.0

    threshold_out = thresholds.get("large_outflow_mnt", 100_000)

    if abs(delta_bal) >= threshold_out:
        kind = "large_outflow" if delta_bal < 0 else "large_inflow"
        events.append(AnomalyEvent(
            wallet_tag=wallet_cfg["tag"],
            wallet_addr=wallet_cfg["address"],
            rationale=wallet_cfg["rationale"],
            kind=kind,
            before={"balance_mnt": prev_bal, "tx_count": prev_tx},
            after={"balance_mnt": cur.balance_mnt, "tx_count": cur.tx_count},
            delta={"mnt": delta_bal, "tx": delta_tx, "pct_balance": round(pct, 4)},
        ))
    return events


def poll_once(rpc: ResilientRPC, send_alerts: bool = True) -> list[AnomalyEvent]:
    """One pass over the watchlist. Returns the list of events emitted."""
    watchlist = load_watchlist()
    state = load_state()
    events: list[AnomalyEvent] = []

    for w in watchlist["wallets"]:
        addr = w["address"]
        cur = snapshot_wallet(rpc, addr)
        if cur is None:
            print(f"  [skip] {w['tag']:<24} — RPC chain exhausted")
            continue
        prev = state.get(addr, {})
        new_events = detect_anomalies(w, prev, cur, watchlist["anomaly_thresholds"])
        events.extend(new_events)
        # persist
        state[addr] = asdict(cur)
        if new_events:
            for ev in new_events:
                print(f"  [ALERT] {w['tag']:<24} {ev.kind}  Δ={ev.delta['mnt']:+,.2f} MNT")
        else:
            print(f"  [ok]    {w['tag']:<24} bal={cur.balance_mnt:>14,.2f} MNT  tx={cur.tx_count}")

    save_state(state)
    return events


if __name__ == "__main__":
    rpc = ResilientRPC()
    print(f"=== poll @ {time.strftime('%H:%M:%S')} ===")
    events = poll_once(rpc)
    print(f"emitted {len(events)} anomaly event(s)")
    print()
    print(rpc.scorecard.render())
    rpc.close()
