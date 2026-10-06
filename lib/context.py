"""The objects every handler needs, bundled once instead of living in module globals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Settings
from .llm.base import Backend
from .mcp.server import Registry
from .store import Store


@dataclass
class Ctx:
    st: Settings
    store: Store
    backend: Backend
    registry: Registry
    bot: Any = None  # telegram.Bot, set at startup

    @property
    def first_name(self) -> str:
        return self.bot.first_name

    @property
    def self_name(self) -> str:
        """How this bot's own messages are labelled in history; must match msgtext.sender_name."""
        return f"{self.bot.first_name} (@{self.bot.username})"

    def now(self) -> str:
        return datetime.now(self.st.tz).strftime("%A %d %B %Y, %H:%M %Z")
