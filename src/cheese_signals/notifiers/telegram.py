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

    def send_photo(self, image: bytes, caption: str = "",
                   filename: str = "chart.png") -> tuple[bool, str]:
        """Send an image with the alert written underneath it.

        One message rather than two, because a picture and its numbers arriving
        separately can be reordered by the network, and a chart with no prices
        under it is a puzzle.

        Telegram caps a photo caption at 1024 characters, which a full alert can
        exceed; over that it is sent as a document, whose caption limit is the
        same but whose failure mode is visible rather than a silent HTTP 400.
        The caption is sent as plain text: Markdown parse failures here would
        cost the image as well as the formatting.
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        caption = caption[:1024]
        try:
            resp = requests.post(
                url,
                data={"chat_id": self.chat_id, "caption": caption},
                files={"photo": (filename, image, "image/png")},
                timeout=max(self.timeout, 30),      # an upload, not a message
            )
            if not resp.ok:
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
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
        # A recovery win is a different thing from a first-entry win: it took
        # two stakes to get one payout, so netting is smaller. Labelling them
        # the same would make the Telegram feed read better than the account.
        step = int((getattr(sig, "features", None) or {}).get("martingale_step", 0))
        if getattr(outcome, "refunded", False):
            # Flat close: Pocket Option returns the stake. Reporting this as a
            # loss made the feed disagree with the account balance.
            icon = "➖ *REFUND*"
        elif outcome.won:
            icon = "✅ *WIN* \\(recovery\\)" if step else "✅ *WIN*"
        else:
            icon = "❌ *LOSS* \\(after recovery\\)" if step else "❌ *LOSS*"
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
            f"P/L: *{outcome.pnl:+.2f}*"
            + (f"   _\\(martingale step {step}, {2 ** step}x stake\\)_" if step else "")
            + f"\n_{_esc(outcome.reason)}_"
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


def discover_chat_ids(bot_token: str, timeout: int = 10) -> tuple[list[dict], str]:
    """Chat IDs that have recently messaged this bot.

    Finding a chat ID is the step that stops people setting Telegram up: the
    number is nowhere in the Telegram interface, and the usual instructions send
    you to a third-party bot to fetch it. It is available from the bot's own
    ``getUpdates``, so the app can just look it up -- provided the user has sent
    the bot one message first, which is the one thing that cannot be automated
    because Telegram will not let a bot open a conversation.

    Returns ``(chats, error)``: a list of ``{"id", "name"}`` and an empty error,
    or an empty list and a reason.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return [], str(exc)
    if resp.status_code == 404:
        return [], "that bot token was rejected by Telegram (404)"
    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        payload = resp.json()
    except ValueError:
        return [], "Telegram returned something that was not JSON"
    if not payload.get("ok"):
        return [], str(payload.get("description") or "Telegram rejected the request")

    seen: dict[str, dict] = {}
    for update in payload.get("result") or []:
        # A chat can arrive under several update kinds; the shape is the same.
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if not chat:
                continue
            chat_id = str(chat.get("id"))
            name = (chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or chat.get("username") or "chat")
            seen[chat_id] = {"id": chat_id, "name": name}
    if not seen:
        return [], ("no messages found. Open Telegram, send your bot any message "
                    "(even 'hi'), then press this again -- a bot cannot start a "
                    "conversation, so it has nothing to read until you do.")
    return list(seen.values()), ""
