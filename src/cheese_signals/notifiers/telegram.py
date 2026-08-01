"""Telegram notifications: advance signal alerts and post-expiry results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import requests


def _esc(text: str) -> str:
    """Escape the characters Telegram's legacy Markdown parser treats specially."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, text: str) -> bool:
        """Send a message. Returns False on failure rather than raising.

        A notification failure must never take the trading loop down with it,
        so network errors are swallowed here and surfaced via the return value.
        """
        try:
            resp = requests.post(
                self._url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def test(self) -> tuple[bool, str]:
        """Used by the Settings screen's 'Send test message' button."""
        ok = self.send("*Cheese Signals* connected. Notifications are working.")
        return (ok, "Test message sent." if ok else "Failed -- check the token and chat ID.")

    # --------------------------- signal alerts ---------------------------
    def send_signal(self, signal) -> bool:
        """Advance warning: pair, direction, and the exact minute to enter."""
        entry = signal.entry_at.astimezone(timezone.utc)
        expiry = signal.expiry_at.astimezone(timezone.utc)
        lead = max(int(signal.seconds_until_entry(datetime.now(timezone.utc))), 0)
        arrow = "🟢 BUY (CALL)" if signal.direction == 1 else "🔴 SELL (PUT)"

        text = (
            f"*{arrow}*\n"
            f"Pair: *{_esc(signal.asset)}*\n"
            f"Enter at: *{entry:%H:%M:%S} UTC*  (in {lead // 60}m {lead % 60}s)\n"
            f"Expiry: *{expiry:%H:%M:%S} UTC*\n"
            f"Confidence: *{signal.score:.0%}*\n"
            f"Setup: {_esc(signal.strategy)}\n"
            f"_{_esc(signal.reason)}_"
        )
        return self.send(text)

    def send_cancelled(self, signal, reason: str) -> bool:
        return self.send(
            f"⚪️ *CANCELLED* {_esc(signal.asset)} {signal.side} "
            f"(was due {signal.entry_at:%H:%M:%S} UTC)\n_{_esc(reason)}_"
        )

    # --------------------------- result alerts ---------------------------
    def send_result(self, outcome) -> bool:
        """Post-expiry win/loss with the attributed reason."""
        sig = outcome.signal
        icon = "✅ *WIN*" if outcome.won else "❌ *LOSS*"
        text = (
            f"{icon} — {_esc(sig.asset)} {sig.side}\n"
            f"Entry {outcome.entry_price:.5f} → Exit {outcome.exit_price:.5f} "
            f"({outcome.move_pips:+.1f} pips)\n"
            f"P/L: *{outcome.pnl:+.2f}*\n"
            f"_{_esc(outcome.reason)}_"
        )
        return self.send(text)

    def send_daily_summary(self, wins: int, losses: int, pnl: float, payout: float = 0.85) -> bool:
        total = wins + losses
        wr = wins / total if total else 0.0
        be = 1.0 / (1.0 + payout)
        verdict = "above" if wr > be else "below"
        return self.send(
            f"📊 *Daily summary*\n"
            f"{wins}W / {losses}L — win rate *{wr:.1%}*\n"
            f"Break-even needed: {be:.1%} ({verdict} break-even)\n"
            f"Net P/L: *{pnl:+.2f}*"
        )
