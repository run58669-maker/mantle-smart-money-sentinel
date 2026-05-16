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


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    rpc = ResilientRPC()
    # Bybit-9 is the only watchlist wallet with non-trivial tx volume on Mantle
    wallet = "0x0d4dc3b8becc98782309e443a6da4b9455b5ca48"
    print(f"scanning last 30 blocks for {wallet}...")
    details = find_recent_wmnt_transfers(rpc, wallet, blocks_back=30)
    print(f"found {len(details)} WMNT transfers")
    for d in details[:5]:
        print(f"  blk={d.block:<12} log={d.log_index:<3} {d.direction:<3} "
              f"counterparty={d.counterparty[:10]}... {d.amount_mnt:,.4f} {d.token}")
    print()
    print(rpc.scorecard.render())
    rpc.close()
