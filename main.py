"""Mantle Smart-Money Sentinel — end-to-end loop.

Tie monitor + llm_interpret + tg_bot together. On each tick:
  1. poll watchlist via ResilientRPC (with breaker/retry/fallback)
  2. diff against on-disk state, emit AnomalyEvent objects
  3. for each event: ask ResilientLLM to write an analyst note
  4. for each event: push that note + raw delta to Telegram

Usage:
    python main.py            # loop forever, 60s interval
    python main.py --once     # one pass and exit (CI / cron friendly)
    python main.py --demo     # forge an anomaly to verify the e2e path
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from llm_interpret import interpret
from mantle_rpc import ResilientRPC
from monitor import STATE_FILE, poll_once
from resilient_llm import Scorecard
from tg_bot import TGBot
from tx_detail import enrich_anomaly

ROOT = Path(__file__).parent

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)


def fire(event, llm_scorecard: Scorecard, rpc: ResilientRPC) -> None:
    # enrich with tx-level detail so the alert can name the destination
    enrich = enrich_anomaly(rpc, event.wallet_addr, blocks_back=60)
    details_lines = [
        f"Δ = <b>{event.delta['mnt']:+,.2f} MNT</b> "
        f"({event.delta.get('pct_balance', 0):+.4f}%)",
        f"tx_count: {event.before['tx_count']} → {event.after['tx_count']}",
        f"Balance: {event.before['balance_mnt']:,.2f} → {event.after['balance_mnt']:,.2f} MNT",
    ]
    if enrich:
        details_lines.append(
            f"Recent tx: {enrich.direction} <code>{enrich.counterparty}</code> "
            f"{enrich.amount_mnt:,.4f} {enrich.token} (blk {enrich.block})"
        )
    details = "\n".join(details_lines)
    interpretation, meta = interpret(event, scorecard=llm_scorecard)
    bot = TGBot()
    ok, info = bot.send_alert(
        wallet_tag=event.wallet_tag,
        wallet_addr=event.wallet_addr,
        kind=event.kind,
        details=details,
        interpretation=interpretation,
    )
    via = meta.get("target", "?")
    lat = meta.get("latency_ms", 0)
    print(f"  → TG msg {info} | LLM via {via} in {lat:.0f}ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--demo", action="store_true",
                    help="forge a -10M MNT outflow on whale-1 to exercise the e2e path")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    if args.demo:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        state.setdefault("0xf22943d05ab93f63b0a229b12f4425e72a4c1f1c", {})
        state["0xf22943d05ab93f63b0a229b12f4425e72a4c1f1c"]["balance_mnt"] = 110_000_006.24
        state["0xf22943d05ab93f63b0a229b12f4425e72a4c1f1c"]["tx_count"] = 2
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print("[demo] forged whale-1 prev balance = 110M; next poll fires alert")

    rpc = ResilientRPC()
    llm_scorecard = Scorecard()

    try:
        while True:
            print(f"\n=== poll @ {time.strftime('%H:%M:%S')} ===")
            events = poll_once(rpc)
            for ev in events:
                fire(ev, llm_scorecard, rpc)

            if args.once or args.demo:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n" + rpc.scorecard.render())
        if llm_scorecard.calls:
            print("LLM " + llm_scorecard.render())
        rpc.close()


if __name__ == "__main__":
    main()
