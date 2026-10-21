"""Connects to a `signal-cli daemon` over its JSON-RPC socket: messages come in as `receive`
notifications, replies go out as `send` requests, all on one connection. The daemon is run by you,
not by the bot."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("bot")
RPC_TIMEOUT = 60  # seconds to wait for signal-cli to answer a request
CONNECT_WAIT = 5  # seconds to keep trying to reach the socket before backing off
LINE_LIMIT = 2**24  # an envelope with attachment details can be far longer than asyncio's 64 KiB default


class SignalError(RuntimeError):
    """signal-cli answered a request with an error."""


class SignalClient:
    def __init__(self, socket_path: str, on_message: Callable[[dict], Awaitable[None]] | None = None):
        self.socket_path = socket_path
        self.on_message = on_message
        self.on_connect: Callable[[], Awaitable[None]] | None = None  # runs after every (re)connection
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Stay connected, reconnecting with a growing pause when the daemon is down or restarts."""
        delay = 5
        while True:
            try:
                if await self._session():
                    delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Signal: %s", e)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(SignalError("signal-cli stopped"))
            self._pending.clear()
            log.warning("Signal: lost signal-cli; trying again in %d s", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _session(self) -> bool:
        """One connection's lifetime. True if it got as far as connecting."""
        reader, writer = await self._connect()
        await self._serve(reader, writer)
        return True

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read the daemon's lines until it closes the connection."""
        self._writer = writer
        log.info("Signal: connected to signal-cli")
        hook = asyncio.create_task(self._on_connect()) if self.on_connect else None
        try:
            async for line in reader:
                self._dispatch(line)
        finally:
            self._writer = None
            if hook:
                hook.cancel()
            writer.close()

    async def _on_connect(self) -> None:
        try:
            await self.on_connect()
        except Exception as e:
            log.error("Signal: %s", e)

    async def _connect(self, wait: int = CONNECT_WAIT) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        for _ in range(wait * 2):
            try:
                return await asyncio.open_unix_connection(self.socket_path, limit=LINE_LIMIT)
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.5)
            except PermissionError as e:
                raise RuntimeError(f"can't open {self.socket_path}: {e}. The bot's user needs write access to "
                                   "the socket and to the directory it is in") from e
        raise RuntimeError(f"couldn't connect to signal-cli's socket {self.socket_path} within {wait} s")

    def _dispatch(self, line: bytes) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        fut = self._pending.pop(obj["id"], None) if isinstance(obj.get("id"), int) else None
        if fut is not None:
            if "error" in obj:
                err = obj["error"]
                fut.set_exception(SignalError(str(err.get("message", err)) if isinstance(err, dict) else str(err)))
            else:
                fut.set_result(obj.get("result"))
        elif self.on_message and (obj.get("method") == "receive" or "envelope" in obj):
            task = asyncio.create_task(self._handle(obj))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _handle(self, obj: dict) -> None:
        try:
            await self.on_message(obj)
        except Exception:
            log.exception("Signal: couldn't handle a message")

    async def request(self, method: str, params: dict):
        if self._writer is None:
            raise SignalError("signal-cli isn't connected")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            self._writer.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": rid}).encode() + b"\n")
            await self._writer.drain()
            return await asyncio.wait_for(fut, RPC_TIMEOUT)
        finally:
            self._pending.pop(rid, None)
