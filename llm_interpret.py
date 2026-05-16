"""LLM interpretation layer — wraps ResilientLLM with a Mantle-specific prompt.

Takes a structured anomaly event + recent context, returns a short
narrative for the Telegram alert: what likely happened, why it matters,
and a calibrated confidence ("flag" vs "high signal" vs "panic"). Falls
back through the resilient chain if any LLM target browns out.
"""
from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass

from dotenv import load_dotenv

from resilient_llm import ResilientLLM, Scorecard, Target

load_dotenv()

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
TFY_URL = os.environ.get("TFY_GATEWAY_URL", "https://gateway.truefoundry.ai")
TFY_KEY = os.environ.get("TFY_API_KEY", "")

# Same chain shape as the TF hackathon project: TF Gateway primary, raw
# Groq as last-resort if TF Gateway browns out.
def build_targets() -> list[Target]:
    targets = []
    if TFY_KEY:
        # 70b primary — smaller model hallucinated numbers on first prompt
        # spec; 70b follows the "use only input numbers" rule more reliably.
        targets.extend([
            Target(name="tfy-groq-70b", base_url=TFY_URL, api_key=TFY_KEY,
                   model="groq/llama-3.3-70b-versatile", max_retries=2),
            Target(name="tfy-groq-8b", base_url=TFY_URL, api_key=TFY_KEY,
                   model="groq/llama-3.1-8b-instant", max_retries=1),
        ])
    if GROQ_KEY:
        targets.append(Target(
            name="raw-groq-8b",
            base_url="https://api.groq.com/openai/v1",
            api_key=GROQ_KEY,
            model="llama-3.1-8b-instant",
            max_retries=1,
        ))
    return targets


SYSTEM_PROMPT = textwrap.dedent("""\
    You are a Mantle on-chain analyst writing one short Telegram alert per
    smart-money anomaly. Keep it to exactly 3 short bullets — nothing else.

    1. WHAT happened — restate the delta verbatim from the input. Do NOT
       round, do NOT convert units, do NOT invent timeframes ("first in 2
       years" is FORBIDDEN unless the input contains a wallet_age field).
    2. WHY it matters — one sentence referencing only the wallet's stated
       role/context. Do NOT speculate beyond that role.
    3. CONFIDENCE — one of: "flag" (notice, don't trade) / "high signal"
       (worth investigating) / "panic" (act now). Justify in <= 8 words.

    HARD RULES:
    - Use ONLY numbers that appear in the input. If you write a number that
      is not in the input, you have failed.
    - Tone: institutional desk note. No emojis. No hype. No claims about
      wallet age, prior frequency, or external markets unless given.
    - If input is sparse, the CONFIDENCE bullet must say "low — sparse input".
""")


@dataclass
class AnomalyEvent:
    wallet_tag: str
    wallet_addr: str
    rationale: str
    kind: str               # e.g. "large_outflow", "dormancy_break"
    before: dict
    after: dict
    delta: dict


def interpret(event: AnomalyEvent, scorecard: Scorecard | None = None) -> tuple[str, dict]:
    targets = build_targets()
    if not targets:
        return "(no LLM target configured — set TFY_API_KEY or GROQ_API_KEY)", {}

    client = ResilientLLM(targets, scorecard=scorecard)
    user_msg = textwrap.dedent(f"""\
        Wallet: {event.wallet_addr} ({event.wallet_tag})
        Role / context: {event.rationale}
        Anomaly kind: {event.kind}

        Before:  {event.before}
        After:   {event.after}
        Delta:   {event.delta}
    """)
    resp, rec = client.chat(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user",   "content": user_msg}],
        max_tokens=220,
        temperature=0.1,
    )
    if not rec.ok:
        return "(LLM chain exhausted — flag manually)", {"ok": False}
    text = resp.choices[0].message.content.strip()
    return text, {"ok": True, "target": rec.final_target, "latency_ms": rec.user_latency_ms}


if __name__ == "__main__":
    ev = AnomalyEvent(
        wallet_tag="whale-1",
        wallet_addr="0xf22943d05ab93f63b0a229b12f4425e72a4c1f1c",
        rationale="Top MNT holder, ~100M MNT, only 2 prior txs — effectively dormant.",
        kind="large_outflow",
        before={"balance_mnt": 100_000_006.24, "tx_count": 2},
        after={"balance_mnt": 99_500_000.00, "tx_count": 3},
        delta={"mnt": -500_006.24, "tx": 1, "pct_balance": -0.5},
    )
    text, meta = interpret(ev)
    print("=== LLM interpretation ===")
    print(text)
    print()
    print("meta:", meta)
