"""SIGNALCLI_SOCKET: answer Signal DMs and group mentions through a signal-cli daemon you run."""

from __future__ import annotations

import logging

from ..context import Ctx
from .client import SignalClient
from .handlers import SignalHandlers

log = logging.getLogger("bot")


async def run(ctx: Ctx) -> None:
    """Stay connected to the daemon's socket until cancelled."""
    client = SignalClient(ctx.st.signal_socket)
    handlers = SignalHandlers(ctx, client)
    client.on_message = handlers.on_message
    client.on_connect = handlers.identify  # asks the daemon whose account it is
    log.info("Signal: connecting to the signal-cli daemon at %s", ctx.st.signal_socket)
    await client.run()
