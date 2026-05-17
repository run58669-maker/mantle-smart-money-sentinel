"""Enrich an AnomalyEvent with concrete transaction detail.

When the monitor sees a wallet's balance jump or `tx_count` increment, it
knows *that* something happened but not *what*. This module pulls the
recent block range, scans WMNT ERC-20 Transfer events for the wallet,
and returns the destination address + amount of the most recent transfer.
Then the LLM alert can say "→ Merchant Moe pool 0x...AB12 for 250,000 MNT"
instead of just "balance changed by -250k".

Public-RPC log windows are typically capped at 1k–10k blocks. We default
to looking ~30 blocks back (≈60s at ~2s blocks) which is enough to catch
anything monitor's poll loop sees. Falls back through the same chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mantle_rpc import (
    ResilientRPC,
    TRANSFER_TOPIC,
    WMNT_ADDRESS,
    address_to_topic,
    topic_to_address,
)


@dataclass
class TransferDetail:
    direction: str          # "out" or "in"
    counterparty: str       # the other side of the transfer
    amount_wei: int
    token: str              # "MNT" (native) or "WMNT" (wrapped) or contract addr
    block: int
    log_index: int

    @property
    def amount_mnt(self) -> float:
        return self.amount_wei / 1e18


def find_recent_wmnt_transfers(
    rpc: ResilientRPC,
    wallet: str,
    blocks_back: int = 30,
) -> list[TransferDetail]:
    """Pull WMNT Transfer events touching `wallet` in the last N blocks."""
    head, _ = rpc.block_number()
    if head is None:
        return []
    from_block = max(0, head - blocks_back)
    wallet_topic = address_to_topic(wallet)

    # As sender (topic[1] = from)
    sent_logs, _ = rpc.logs(
        from_block=from_block, to_block=head, address=WMNT_ADDRESS,
        topics=[TRANSFER_TOPIC, wallet_topic],
    )
    # As receiver (topic[2] = to)
    recv_logs, _ = rpc.logs(
        from_block=from_block, to_block=head, address=WMNT_ADDRESS,
        topics=[TRANSFER_TOPIC, None, wallet_topic],
    )

    details: list[TransferDetail] = []
    for log in (sent_logs or []):
        details.append(_to_detail(log, wallet, direction="out"))
    for log in (recv_logs or []):
        details.append(_to_detail(log, wallet, direction="in"))
    # newest first
    details.sort(key=lambda d: (d.block, d.log_index), reverse=True)
    return details


def _to_detail(log: dict, wallet: str, direction: str) -> TransferDetail:
    topics = log["topics"]
    counterparty = topic_to_address(topics[2] if direction == "out" else topics[1])
    amount = int(log["data"], 16)
    return TransferDetail(
        direction=direction,
        counterparty=counterparty,
        amount_wei=amount,
        token="WMNT",
        block=int(log["blockNumber"], 16),
        log_index=int(log["logIndex"], 16),
    )


def summarize_recent(rpc: ResilientRPC, wallet: str, blocks_back: int = 30) -> Optional[TransferDetail]:
    """Return the most recent transfer touching `wallet`, or None if no logs."""
    details = find_recent_wmnt_transfers(rpc, wallet, blocks_back)
    return details[0] if details else None


# === Native MNT transfer scan ============================================== #
# WMNT logs miss native MNT moves (CEX wallets typically move native, not
# wrapped). For full coverage we scan recent blocks and filter txs by from/to.
# Cost: one RPC call per block × `blocks_back` — acceptable at default 30.

def find_recent_native_transfers(
    rpc: ResilientRPC,
    wallet: str,
    blocks_back: int = 30,
) -> list[TransferDetail]:
    """Pull native MNT transfers touching `wallet` in the last N blocks.

    Iterates blocks newest-first and stops early once we have one match
    (since the caller usually only needs the most recent). Tx value is in
    wei, returned as TransferDetail with token='MNT'.
    """
    head, _ = rpc.block_number()
    if head is None:
        return []
    wallet_lc = wallet.lower()
    details: list[TransferDetail] = []
    # newest first so the caller's [0] is the most recent
    for bn in range(head, max(0, head - blocks_back), -1):
        block, _ = rpc.block_by_number(bn, with_txs=True)
        if not block or "transactions" not in block:
            continue
        for tx in block["transactions"]:
            if not isinstance(tx, dict):
                continue  # bare hash, with_txs=False fallback — skip
            tx_from = (tx.get("from") or "").lower()
            tx_to = (tx.get("to") or "").lower()
            if tx_from != wallet_lc and tx_to != wallet_lc:
                continue
            value_wei = int(tx.get("value", "0x0"), 16)
            if value_wei == 0:
                # contract call / approval, no MNT moved; skip for now
                continue
            direction = "out" if tx_from == wallet_lc else "in"
            counterparty = tx_to if direction == "out" else tx_from
            details.append(TransferDetail(
                direction=direction,
                counterparty=counterparty,
                amount_wei=value_wei,
                token="MNT",
                block=bn,
                log_index=int(tx.get("transactionIndex", "0x0"), 16),
            ))
    return details


def enrich_anomaly(rpc: ResilientRPC, wallet: str, blocks_back: int = 30) -> Optional[TransferDetail]:
    """Try WMNT first (cheap, single log query), fall back to native scan.

    Returns the single most recent transfer touching `wallet`, regardless
    of whether it was a WMNT Transfer event or a native MNT tx.
    """
    wmnt = find_recent_wmnt_transfers(rpc, wallet, blocks_back)
    if wmnt:
        return wmnt[0]
    native = find_recent_native_transfers(rpc, wallet, blocks_back)
    return native[0] if native else None


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    rpc = ResilientRPC()
    # Bybit-9 has the most tx volume in the watchlist
    wallet = "0x0d4dc3b8becc98782309e443a6da4b9455b5ca48"

    print(f"=== WMNT log scan for {wallet} ===")
    wmnt = find_recent_wmnt_transfers(rpc, wallet, blocks_back=30)
    print(f"found {len(wmnt)} WMNT Transfer events")
    for d in wmnt[:3]:
        print(f"  blk={d.block} log={d.log_index} {d.direction} "
              f"counterparty={d.counterparty[:10]}... {d.amount_mnt:,.4f} {d.token}")

    print(f"\n=== native MNT scan for {wallet} ===")
    native = find_recent_native_transfers(rpc, wallet, blocks_back=15)
    print(f"found {len(native)} native MNT txs")
    for d in native[:5]:
        print(f"  blk={d.block} idx={d.log_index} {d.direction} "
              f"counterparty={d.counterparty[:10]}... {d.amount_mnt:,.4f} {d.token}")

    print(f"\n=== unified enrich_anomaly ===")
    best = enrich_anomaly(rpc, wallet, blocks_back=15)
    print('most recent transfer:', best)

    print()
    print(rpc.scorecard.render())
    rpc.close()
