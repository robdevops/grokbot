import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from tgbot import app, config
from tgbot.context import Ctx
from tgbot.llm.openrouter import OpenRouterBackend
from tgbot.llm.xai import XaiBackend
from tgbot.mcp.server import MCPServer

from .conftest import FakeBot
from .fakes import ScriptedBackend, registry


def handler_kinds(application):
    return [type(h).__name__ for h in application.handlers[0]]


def test_backend_follows_the_key(env):
    assert isinstance(app.build_backend(config.load(env)), OpenRouterBackend)
    xai = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "k"})
    assert isinstance(app.build_backend(xai), XaiBackend)


def test_handlers_follow_the_flags(env, store):
    def build(**extra):
        st = config.load({**env, **extra})
        return app.build_app(Ctx(st, store, ScriptedBackend([]), registry())), st

    plain, st = build()
    assert handler_kinds(plain) == ["MessageHandler"] and app.allowed_updates(st) == ["message", "edited_message"]
    full, st = build(OWNER_USER_ID="5", SHARESIGHT_HOLDING_NEWS_RECIPIENTS="Pf:alice")
    kinds = handler_kinds(full)
    assert kinds == ["CommandHandler", "CommandHandler", "MessageHandler", "CallbackQueryHandler"]
    assert "callback_query" in app.allowed_updates(st)
    assert {c for h in full.handlers[0] if isinstance(h, CommandHandler) for c in h.commands} == {"credits", "usage"}
    assert any(isinstance(h, MessageHandler) for h in full.handlers[0])
    assert any(isinstance(h, CallbackQueryHandler) for h in full.handlers[0])


def test_main_refuses_to_start_with_both_or_neither_key(monkeypatch):
    for k in ("XAI_API_KEY", "OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    with pytest.raises(SystemExit, match="Set XAI_API_KEY"):
        app.main([])
    monkeypatch.setenv("XAI_API_KEY", "a")
    monkeypatch.setenv("OPENROUTER_API_KEY", "b")
    with pytest.raises(SystemExit, match="Both"):
        app.main(["mimo"])


def test_main_accepts_and_ignores_a_label_and_unknown_args(monkeypatch, tmp_path):
    started = {}
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "b")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.setattr(app.Application, "run_polling", lambda self, **kw: started.update(kw))
    app.main(["mimo", "--whatever"])
    assert started["allowed_updates"] == ["message", "edited_message"]
    started.clear()
    app.main([])
    assert started


async def test_alert_down_notifies_admin_chats_only(env, store):
    st = config.load({**env, "ADMIN_CHAT_IDS": "-10 -11"})
    ctx = Ctx(st, store, ScriptedBackend([]), registry(), FakeBot())
    server = MCPServer("yahoo", {}, timeout=1, max_output=10)
    server.error = "boom <x>"
    await app.alert_down(ctx, server)
    assert sorted(s["chat_id"] for s in ctx.bot.sent) == [-11, -10]
    assert "<b>yahoo</b>" in ctx.bot.sent[0]["text"] and "boom &lt;x&gt;" in ctx.bot.sent[0]["text"]
    assert store.history(-10, 20)[0].text.startswith("⚠️")
    quiet = Ctx(config.load(env), store, ScriptedBackend([]), registry(), FakeBot())
    await app.alert_down(quiet, server)
    assert quiet.bot.sent == []
