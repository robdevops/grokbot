"""SIGNALCLI_BIN / SIGNALCLI_SOCKET: answer Signal DMs and group mentions through a signal-cli daemon."""

from __future__ import annotations

import logging

from ..context import Ctx
from .client import SignalClient, list_accounts
from .handlers import SignalHandlers

log = logging.getLogger("bot")


async def run(ctx: Ctx) -> None:
    """Connect to the daemon at SIGNALCLI_SOCKET, or start one from SIGNALCLI_BIN, until cancelled."""
    st = ctx.st
    if st.signal_socket:
        client = SignalClient(socket_path=st.signal_socket)
        handlers = SignalHandlers(ctx, client)
        client.on_connect = handlers.identify  # asks the daemon whose account it is
        log.info("Signal: connecting to the signal-cli daemon at %s", st.signal_socket)
    else:
        try:
            accounts = await list_accounts(st.signal_cli)
        except Exception as e:
            log.error("Signal is off: couldn't list accounts with %s: %s", st.signal_cli, e)
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
        client = SignalClient(st.signal_cli)
        handlers = SignalHandlers(ctx, client, number or aci, aci)
        log.info("Signal: starting for account %s", aci)
    client.on_message = handlers.on_message
    await client.run()
