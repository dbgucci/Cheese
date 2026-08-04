"""Telegram notifications: advance signal alerts and post-expiry results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import requests


def _strip_markdown(text: str) -> str:
    """Plain-text fallback used when Telegram rejects the formatted version."""
    for ch in ("*", "_", "`"):
        text = text.replace(ch, "")
    return text.replace("\\", "")


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
        ok, _ = self.send_verbose(text)
        return ok

    def send_verbose(self, text: str) -> tuple[bool, str]:
        """Send a message, returning (ok, error).

        Telegram rejects a whole message with HTTP 400 if its Markdown does
        not parse, and an attributed win/loss reason is full of underscores,
        parentheses and decimals that can trip it. Losing every result message
        to a formatting error -- silently -- is worse than losing the
        formatting, so a parse failure is retried as plain text.

        Failures are reported rather than swallowed: a notifier that quietly
        does nothing is indistinguishable from a broken bot.
        """
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            resp = requests.post(self._url, json=payload, timeout=self.timeout)
            if resp.status_code == 400:
                plain = {"chat_id": self.chat_id, "text": _strip_markdown(text)}
                retry = requests.post(self._url, json=plain, timeout=self.timeout)
                if retry.ok:
                    return True, "sent as plain text (Markdown was rejected)"
                return False, f"HTTP {retry.status_code}: {retry.text[:200]}"
            resp.raise_for_status()
            return True, ""
        except requests.RequestException as exc:
            return False, str(exc)

    def test(self) -> tuple[bool, str]:
        """Used by the Settings screen's 'Send test message' button."""
        ok = self.send("*KPS* connected. Notifications are working.")
        return (ok, "Test message sent." if ok else "Failed -- check the token and chat ID.")

    # --------------------------- signal alerts ---------------------------
    def send_signal(self, signal) -> bool:
        """Advance warning: pair, direction, and the exact minute to enter."""
        entry = signal.entry_at.astimezone(timezone.utc)
        expiry = signal.expiry_at.astimezone(timezone.utc)
        lead = max(int(signal.seconds_until_entry(datetime.now(timezone.utc))), 0)
        arrow = "🟢 BUY (CALL)" if signal.direction == 1 else "🔴 SELL (PUT)"

        # State the duration, not just the two clock times. This is the number
        # typed into the platform's expiry box, and inferring it by subtracting
        # two timestamps under time pressure is how the wrong one gets set.
        minutes = max(int(round((expiry - entry).total_seconds() / 60)), 1)
        text = (
            f"*{arrow}*\n"
            f"Pair: *{_esc(signal.asset)}*\n"
            f"Enter at: *{entry:%H:%M:%S} UTC*  (in {lead // 60}m {lead % 60}s)\n"
            f"Expiry: *{minutes} min* — closes {expiry:%H:%M:%S} UTC\n"
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
    def send_result(self, outcome) -> tuple[bool, str]:
        """Post-expiry win/loss with the attributed reason.

        Returns (ok, error) so the caller can surface a delivery failure
        instead of the user simply never receiving a result.
        """
        sig = outcome.signal
        icon = "✅ *WIN*" if outcome.won else "❌ *LOSS*"
        entry = sig.entry_at.astimezone(timezone.utc)
        expiry = sig.expiry_at.astimezone(timezone.utc)
        minutes = max(int(round((expiry - entry).total_seconds() / 60)), 1)
        # Name the window this result is for. Without it a result arriving at
        # 06:11 for a trade that expired at 06:10 is impossible to match back
        # to the signal it belongs to.
        text = (
            f"{icon} — {_esc(sig.asset)} {sig.side}\n"
            f"{entry:%H:%M:%S} → {expiry:%H:%M:%S} UTC ({minutes} min)\n"
            f"Entry {outcome.entry_price:.5f} → Exit {outcome.exit_price:.5f} "
            f"({outcome.move_pips:+.1f} pips)\n"
            f"P/L: *{outcome.pnl:+.2f}*\n"
            f"_{_esc(outcome.reason)}_"
        )
        return self.send_verbose(text)

    def send_daily_summary(self, wins: int, losses: int, pnl: float, payout: float = 0.85) -> bool:
        total = wins + losses
        wr = wins / total if total else 0.0
        be = 1.0 / (1.0 + payout)
        verdict = "above" if wr > be else "below"
        return self.send(
            f"📊 *Daily summary*\n"
            f"{wins}W / {losses}L — win rate *{wr:.1%}*\n"
            f"Break-even needed: {be:.1%} ({verdict} break-even)"
        )
