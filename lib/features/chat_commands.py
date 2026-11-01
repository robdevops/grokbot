"""Slash commands configured per MCP server (`chat_commands` in mcp_servers.json): /forecast sydney asks the model,
offered only that server's tool and made to call it."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("bot")

NAME = re.compile(r"^[a-z0-9_]{1,32}$")  # what Telegram accepts as a command name
DESCRIPTION_MAX = 256  # Telegram's limit for a command's menu text


@dataclass(frozen=True)
class ChatCommand:
    name: str  # without the slash
    server: str  # the MCP server's label
    tool: str  # the MCP tool's own name
    require_arg: bool = False
    description: str = ""  # for the command menu


def load(servers: dict[str, dict], reserved: set[str]) -> list[ChatCommand]:
    """The chat commands of every server ({label: cfg}), in file order. A bad entry is skipped with a warning: a name
    Telegram won't take, one the bot already uses, one used twice, or no tool."""
    found: list[ChatCommand] = []
    for label, cfg in servers.items():
        entries = cfg.get("chat_commands") or {}
        if not isinstance(entries, dict):
            log.warning("MCP %s: chat_commands must be an object of {name: {tool, require_arg}}", label)
            continue
        for name, spec in entries.items():
            spec = spec if isinstance(spec, dict) else {}
            tool = str(spec.get("tool") or "").strip()
            problem = ("isn't a valid command name (lowercase letters, digits and _, at most 32)" if not NAME.match(name)
                       else "is one of the bot's own commands" if name in reserved
                       else "is already used by another server" if any(c.name == name for c in found)
                       else "names no tool" if not tool else "")
            if problem:
                log.warning("MCP %s: chat command /%s %s; skipped", label, name, problem)
                continue
            text = str(spec.get("description") or f"{tool} via {label}")[:DESCRIPTION_MAX]
            found.append(ChatCommand(name, label, tool, bool(spec.get("require_arg")), text))
    return found


def args_of(text: str) -> str:
    """What follows the command: "/forecast@bot sydney this weekend" -> "sydney this weekend"."""
    parts = (text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def usage(cmd: ChatCommand) -> str:
    return f"Usage: /{cmd.name} &lt;what to look up&gt;"


def unavailable(cmd: ChatCommand, server_up: bool) -> str:
    if not server_up:
        return f"The {html.escape(cmd.server)} data source isn't available right now."
    return f"/{cmd.name} can't run: the {html.escape(cmd.server)} tool {html.escape(cmd.tool)} isn't enabled."


def hint(cmd: ChatCommand, fn: str, args: str) -> str:
    """The line added to the prompt: which tool to call, for what."""
    asked = f' for what they asked ("{args}")' if args else ""
    return f"\n\nThey used the /{cmd.name} command: call {fn}{asked} and answer from its result."
