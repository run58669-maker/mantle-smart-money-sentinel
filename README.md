# Mantle Smart-Money Sentinel

Submission concept for the **Mantle Turing Test Hackathon 2026 — Track 2 (AI Alpha & Data)**.

A Telegram bot that watches a curated list of Mantle smart-money wallets,
detects anomalies (large outflows / dormancy breaks / cluster moves), and
posts an LLM-written analyst note to a chat — all wrapped in the same
retry → circuit breaker → fallback-chain primitives we built for the
TrueFoundry "Resilient Agents" track. Every layer (RPC, LLM) is
fault-tolerant by default: if the primary Mantle RPC browns out, the
chain falls through to Ankr → publicnode; if the TrueFoundry AI Gateway
itself goes down, the LLM call falls through to a raw Groq endpoint.

## How it answers the prompt

Track 2 asks for "smart money tracking and on-chain anomaly detection
bots via Telegram and Discord." Sentinel ships:

1. **Curated watchlist** of real Mantle whales (verified from
   `mantlescan.xyz` — Bybit-9 hot wallet, Mantle Budget L2 treasury,
   Mantle Rewards Station, plus two top unlabeled $100M+ holders).
2. **ResilientRPC** — three Mantle endpoints in a priority-ordered chain
   with per-target circuit breakers. Demo p95 ≈ 220 ms cold, ≈ 90 ms warm.
3. **State diff anomaly detector** with thresholds (default 100k MNT
   outflow trips an alert). Restart-safe — state persists on disk.
4. **ResilientLLM** for the analyst note. Strict-prompt + t=0.1 + 70b
   primary stops the small model from inventing numbers (real bug we hit
   on first run and tightened).
5. **Telegram outbound** — Bot API `sendMessage`, no inbound MCP plugin
   dependency.

## Quick start

```bash
git clone https://github.com/run58669-maker/mantle-smart-money-sentinel.git
cd mantle-smart-money-sentinel
pip install -r requirements.txt
cp .env.example .env  # fill in GROQ_API_KEY + TFY_API_KEY; TG token has a default
python main.py --demo    # forge a -10M MNT outflow on whale-1, fire one Telegram alert
python main.py --once    # one real poll, no demo forging
python main.py           # loop forever (60 s interval)
```

## Files

```
resilient_llm.py    LLM core: Target / ResilientLLM / _Breaker / Scorecard (shared with TF project)
mantle_rpc.py       ResilientRPC — three Mantle endpoints in a fallback chain
chaos.py            BurstFault / RandomFault / MCPToolFault hooks (shared)
llm_interpret.py    Strict-prompt analyst note generator on top of ResilientLLM
tg_bot.py           Telegram Bot API outbound wrapper
monitor.py          poll → snapshot → diff → emit AnomalyEvent
main.py             end-to-end loop (poll → interpret → send)
watchlist.json      curated smart-money seed list
.env.example        credentials template
```

## Resilience layers (carried over from the TF Resilient Agents submission)

Every external call goes through the same three layers:

```
   ┌──────────────────────┐   ┌──────────────────────┐
   │ ResilientLLM         │   │ ResilientRPC         │
   │ retry → breaker      │   │ retry → breaker      │
   │      → fallback chain │   │      → fallback chain │
   └──────────┬───────────┘   └──────────┬───────────┘
              ▼                            ▼
    tfy-groq-70b  tfy-groq-8b      mantle-public
    raw-groq-8b                     mantle-ankr
                                    mantle-publicnode
```

If any single endpoint browns out, the alert still goes through. If the
TrueFoundry Gateway itself goes down, the LLM layer reaches Groq
directly. If every Mantle RPC is unhealthy, the breakers all open and
the alert layer correctly emits no false positives — silence is the
right answer when state is unknowable.

## Live demo

Run `python main.py --demo` — the script forges a -10M MNT prev-balance
on whale-1, polls, detects the implied outflow, asks Groq via TrueFoundry
to write the note, and posts it to Telegram. End-to-end takes ~1.5
seconds; the unified Scorecard (RPC + LLM) prints at the end.

## Known limits

- Watchlist curation is currently manual. Adding a Nansen API plug-in is
  on the next-iteration list.
- Per-tx detail not yet pulled — anomalies are inferred from balance and
  `tx_count` deltas only. Pulling `eth_getLogs` for the same wallet
  range would let the alert name the destination contract.
- `state.json` lives on local disk; for multi-host deployment swap to
  Redis or a small key-value store.
