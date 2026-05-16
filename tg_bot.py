"""Telegram bot wrapper for Sentinel alerts.

Reuses the bot token already registered for the Claude Code Telegram
channel (so 小Q doesn't need to /newbot). Outbound-only — Sentinel posts
alerts to her chat_id via Telegram Bot API over HTTPS. The Claude Code
Telegram MCP plugin (inbound) is *not* used here; this script talks to
api.telegram.org directly.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx


# Default token + chat_id sourced from the existing telegram channel config.
# Can be overridden via env (.env in this repo).
DEFAULT_TOKEN = "8634391771:AAGJ0UxB0OsXZzIp7JjFE81tToTvpqLpJns"
DEFAULT_CHAT_ID = "8663622105"


@dataclass
class TGBot:
    token: str = ""
    chat_id: str = ""
    timeout_s: float = 8.0

    def __post_init__(self):
        self.token = self.token or os.environ.get("TG_BOT_TOKEN") or DEFAULT_TOKEN
        self.chat_id = self.chat_id or os.environ.get("TG_CHAT_ID") or DEFAULT_CHAT_ID

    def send(self, text: str, parse_mode: str = "HTML") -> tuple[bool, str]:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r = httpx.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode,
                      "disable_web_page_preview": True},
                timeout=self.timeout_s,
            )
            r.raise_for_status()
            return True, r.json().get("result", {}).get("message_id", "?")
        except httpx.HTTPStatusError as e:
            return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def send_alert(
        self,
        wallet_tag: str,
        wallet_addr: str,
        kind: str,
        details: str,
        interpretation: str = "",
    ) -> tuple[bool, str]:
        text = (
            f"🛰 <b>Mantle Sentinel</b>  ·  <i>{kind}</i>\n\n"
            f"<b>Wallet:</b> <code>{wallet_addr}</code>\n"
            f"<b>Tag:</b> {wallet_tag}\n\n"
            f"<b>Detail:</b>\n{details}\n"
        )
        if interpretation:
            text += f"\n<b>LLM read:</b>\n{interpretation}\n"
        text += f"\n<i>at {time.strftime('%Y-%m-%d %H:%M:%S')}</i>"
        return self.send(text)


if __name__ == "__main__":
    bot = TGBot()
    ok, info = bot.send_alert(
        wallet_tag="whale-1",
        wallet_addr="0xf22943d05ab93f63b0a229b12f4425e72a4c1f1c",
        kind="large_outflow",
        details=(
            "Balance dropped <b>100,000,006.24 → 99,500,000.00 MNT</b>\n"
            "Δ = <b>-500,006.24 MNT</b> (~$300k USD est.)\n"
            "tx_count: 2 → 3 (single tx)\n"
            "Block: 95,420,263"
        ),
        interpretation=(
            "First outflow from this $100M wallet since funding. Size is moderate "
            "relative to balance (0.5%) but breaks 100% dormancy. Likely OTC "
            "settlement or first-leg of a position. Worth flagging — not panic."
        ),
    )
    print(f"send_alert -> ok={ok}  info={info}")
