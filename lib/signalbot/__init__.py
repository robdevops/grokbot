"""SIGNALCLI_BIN: answer Signal DMs and group mentions through a signal-cli daemon the bot runs itself."""

from __future__ import annotations

import logging

from ..context import Ctx
from .client import SignalClient, list_accounts
from .handlers import SignalHandlers

log = logging.getLogger("bot")


async def run(ctx: Ctx) -> None:
    """Find the bot's Signal account, then keep the daemon running until cancelled."""
    path = ctx.st.signal_cli
    try:
        accounts = await list_accounts(path)
    except Exception as e:
        log.error("Signal is off: couldn't list accounts with %s: %s", path, e)
        return
    if not accounts:
        log.error("Signal is off: signal-cli has no registered account")
        return
    if len(accounts) > 1:
        log.warning("Signal: %d accounts registered; using the first", len(accounts))
    number, aci = accounts[0].get("number"), accounts[0].get("aci") or accounts[0].get("uuid")
    if not aci:
        log.error("Signal is off: the account has no ACI (listAccounts gave %s)", accounts[0])
        return
    client = SignalClient(path)
    client.on_message = SignalHandlers(ctx, client, number or aci, aci).on_message
    log.info("Signal: starting for account %s", aci)
    await client.run()
