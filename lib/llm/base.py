"""Provider-neutral request/response types and the Backend interface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..mcp.schema import ToolDef


@dataclass
class Usage:
    tokens_in: int = 0
    cached: int = 0
    tokens_out: int = 0
    cost: float = 0.0

    def add(self, other: Usage) -> None:
        self.tokens_in += other.tokens_in
        self.cached += other.cached
        self.tokens_out += other.tokens_out
        self.cost += other.cost

    @property
    def cached_pct(self) -> float:
        return 100 * self.cached / self.tokens_in if self.tokens_in else 0.0


@dataclass
class Call:
    """A function call the model handed back."""

    id: str
    name: str
    arguments: str


@dataclass
class Step:
    """One model response."""

    text: str
    calls: list[Call]
    usage: Usage
    finish: str | None = None
    searches: int = 0  # server-side searches the provider ran for this step
    raw: Any = None  # provider payload add_results() needs to replay this step


@dataclass
class Request:
    """Everything one model question needs; parts are neutral:
    {"type": "text", "text": ...} or {"type": "image", "url": ..., "detail": "low"|"high"}."""

    system: str
    parts: list[dict]
    model: str
    tools: list[ToolDef] = field(default_factory=list)  # MCP tools offered
    search: bool = False  # offer the provider's web search
    must_search: bool = False  # force a search on the first round (tools is then empty)
    reasoning: str = ""
    cache_id: str | None = None  # keeps a chat's requests on the server holding its cached prompt
    on_text: Callable[[str], None] | None = None  # streaming callback, gets the text so far
    no_tools_system: str = ""  # system prompt for the retry without tools
    local: dict[str, Callable[[dict], Awaitable[str]]] = field(default_factory=dict)  # tools run in the bot
    max_tokens: int | None = None  # reply cap incl. reasoning; None = the provider's setting


@dataclass
class Answer:
    text: str
    usage: Usage
    rounds: int
    tool_calls: int


class Backend:
    """What a provider adapter provides. The tool loop, retries and prompts are shared."""

    name = ""
    search_what = ""  # how prompts name the search ("the web and X (Twitter)")

    def start(self, req: Request) -> Any:
        """The opaque conversation state for a new request."""
        raise NotImplementedError

    async def step(self, conv: Any, req: Request, *, tool_choice: str | None) -> Step:
        """Send the conversation and parse the (streamed) response. tool_choice is
        "required" (force a search), "none" (answer now) or None (model decides)."""
        raise NotImplementedError

    def add_results(self, conv: Any, step: Step, results: list[str]) -> None:
        """Append the model's tool calls and their results to the conversation."""
        raise NotImplementedError

    def add_user_message(self, conv: Any, text: str) -> None:
        """Append a plain user turn (used to hand recovered search results back)."""
        raise NotImplementedError

    async def run_search(self, query: str, usage: Usage) -> str | None:
        """Run a search the model handed back as a function call and add its cost to `usage`;
        None if the provider runs searches server-side (then a stray search call is simply
        unavailable)."""
        return None

    async def credits(self) -> str | None:
        """Account balance text, if the provider has one."""
        return None
