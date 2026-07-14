"""Push a signal to a Telegram chat via a bot token."""

from __future__ import annotations

import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, text: str) -> None:
        resp = requests.post(
            self._url,
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        resp.raise_for_status()
