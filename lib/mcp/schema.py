"""Neutral tool definitions and the shrinking of MCP tool schemas (they are re-sent every round)."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("bot")

# Tools whose names start with these are treated as read-only when the server doesn't annotate
# them. Anything else (create_*, delete_*, update_* ...) is left out unless listed in
# "allowed_tools", so nobody in the group can talk the bot into changing your data.
READ_ONLY_PREFIXES = ("get", "list", "search", "fetch", "find", "lookup", "show", "read")
ENV_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")
HIDDEN_PARAMS = {"response_format"}  # optional params the model shouldn't bother with
DESCRIPTION_SKIP = re.compile(r"^(returns?|example|args|arguments|note|raises)\b", re.IGNORECASE)
DROP_KEYS = {"title", "default", "examples", "example"}


@dataclass(frozen=True)
class ToolDef:
    """A function tool in provider-neutral form; each provider wraps it for its own API."""

    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


def compact_description(text: str, limit: int = 220) -> str:
    """Keep the paragraphs saying what a tool does and when to use it; drop ones about return
    formats, examples and the like, squash whitespace and cut to `limit` characters."""
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.strip()]
    kept = [p for i, p in enumerate(paras) if i == 0 or not DESCRIPTION_SKIP.match(p)]
    return " ".join(kept)[:limit]


def compact_schema(schema, param_desc_limit: int = 120):
    """Strip titles/defaults/examples, hide HIDDEN_PARAMS and cut each parameter description to
    its first sentence."""
    if isinstance(schema, list):
        return [compact_schema(x, param_desc_limit) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    required = set(schema.get("required", []))
    for key, value in schema.items():
        if key in DROP_KEYS and not isinstance(value, dict):
            continue
        if key == "properties" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in HIDDEN_PARAMS or k in required}
        if key == "description" and isinstance(value, str):
            value = re.split(r"(?<=[.;])\s", " ".join(value.split()), maxsplit=1)[0][:param_desc_limit]
        out[key] = compact_schema(value, param_desc_limit)
    if "$defs" in out:  # drop definitions nothing refers to any more
        used = json.dumps({k: v for k, v in out.items() if k != "$defs"})
        out["$defs"] = {k: v for k, v in out["$defs"].items() if f"#/$defs/{k}" in used}
        if not out["$defs"]:
            del out["$defs"]
    return out


def looks_read_only(tool) -> bool:
    hint = getattr(tool.annotations, "readOnlyHint", None) if tool.annotations else None
    if hint is not None:
        return hint
    return tool.name.lower().startswith(READ_ONLY_PREFIXES)


def expand_env(d: dict | None, label: str = "") -> dict | None:
    """Expand ${VAR} references so secrets can stay in the environment, not the JSON.

    Unset variables become empty strings (and are logged), so the server fails at startup with
    its own "missing credentials" error instead of accepting a literal "${VAR}"."""
    if not d:
        return None
    missing: set[str] = set()

    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            missing.add(name)
        return os.environ.get(name, "")

    out = {k: ENV_REF.sub(sub, str(v)) for k, v in d.items()}
    if missing:
        log.warning("MCP %s: environment variable(s) not set: %s", label, ", ".join(sorted(missing)))
    return out
