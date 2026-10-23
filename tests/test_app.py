import asyncio
from types import SimpleNamespace

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from lib import app, config
from lib.context import Ctx
from lib.llm.openrouter import OpenRouterBackend
from lib.llm.xai import XaiBackend
from lib.llm.zai import ZaiBackend
from lib.mcp.server import MCPServer

from .conftest import FakeBot
from .fakes import ScriptedBackend, registry


def handler_kinds(application):
    return [type(h).__name__ for h in application.handlers[0]]


def test_backend_follows_the_key(env):
    assert isinstance(app.build_backend(config.load(env)), OpenRouterBackend)
    xai = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "k"})
    assert isinstance(app.build_backend(xai), XaiBackend)
    zai = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "ZAI_API_KEY": "k"})
    assert isinstance(app.build_backend(zai), ZaiBackend)


def test_handlers_follow_the_flags(env, store):
    def build(**extra):
        st = config.load({**env, **extra})
        return app.build_app(Ctx(st, store, ScriptedBackend([]), registry())), st

    plain, st = build()
    assert handler_kinds(plain) == ["CommandHandler", "CommandHandler", "MessageHandler"] and app.allowed_updates(st) == ["message", "edited_message"]
    full, st = build(ADMIN_CHAT_IDS="5", SHARESIGHT_HOLDING_NEWS_RECIPIENTS="Pf:alice")
    kinds = handler_kinds(full)
    assert kinds == ["CommandHandler", "CommandHandler", "MessageHandler", "CallbackQueryHandler"]
    assert "callback_query" in app.allowed_updates(st)
    assert {c for h in full.handlers[0] if isinstance(h, CommandHandler) for c in h.commands} == {"credits", "flushcache"}
    assert any(isinstance(h, MessageHandler) for h in full.handlers[0])
    locked, st = build(ADMIN_ONLY="on")  # remembers the groups it is in, to know who their admins are
    assert "ChatMemberHandler" in handler_kinds(locked) and "my_chat_member" in app.allowed_updates(st)
    assert any(isinstance(h, CallbackQueryHandler) for h in full.handlers[0])


def test_main_refuses_to_start_with_both_or_neither_key(monkeypatch):
    for k in ("XAI_API_KEY", "OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    with pytest.raises(SystemExit, match="Set XAI_API_KEY"):
        app.main([])
    monkeypatch.setenv("XAI_API_KEY", "a")
    monkeypatch.setenv("OPENROUTER_API_KEY", "b")
    with pytest.raises(SystemExit, match="only one"):
        app.main(["mimo"])


def test_main_accepts_and_ignores_a_label_and_unknown_args(monkeypatch, tmp_path):
    started = {}
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "b")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("ADMIN_ONLY", "off")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setattr(config, "MCP_CONFIG", tmp_path / "none.json")
    monkeypatch.setattr(app.Application, "run_polling", lambda self, **kw: started.update(kw))
    app.main(["mimo", "--whatever"])
    assert started["allowed_updates"] == ["message", "edited_message"]
    started.clear()
    app.main([])
    assert started


def test_startup_logs_the_git_hash_and_whether_the_backfill_is_on(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "b")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setattr(config, "MCP_CONFIG", tmp_path / "none.json")
    monkeypatch.setattr(app.Application, "run_polling", lambda self, **kw: None)
    caplog.set_level("INFO", logger="bot")
    monkeypatch.setattr(app, "git_hash", lambda: "abc1234")
    monkeypatch.setenv("PERIOD_BACKFILL", "off")
    app.main(["grokbot"])
    assert "Starting instance grokbot (abc1234): " in caplog.text and "Period backfill" not in caplog.text
    caplog.clear()
    monkeypatch.setattr(app, "git_hash", lambda: "")
    monkeypatch.setenv("PERIOD_BACKFILL", "RobSMSF, other")
    app.main(["grokbot"])
    assert "Starting instance grokbot: " in caplog.text
    assert "Period backfill: on for other, robsmsf" in caplog.text


def test_git_hash_is_read_from_the_git_directory_without_running_git(tmp_path):
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert app.git_hash(tmp_path) == ""  # no checkout
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    assert app.git_hash(tmp_path) == ""  # the branch has no commit yet
    (git / "refs" / "heads" / "main").write_text(sha + "\n")
    assert app.git_hash(tmp_path) == "0123456"
    (git / "refs" / "heads" / "main").unlink()  # packed by gc
    (git / "packed-refs").write_text(f"# pack-refs\n{sha} refs/heads/main\n")
    assert app.git_hash(tmp_path) == "0123456"
    (git / "HEAD").write_text(sha + "\n")  # detached
    assert app.git_hash(tmp_path) == "0123456"
    (git / "HEAD").write_text("garbage")
    assert app.git_hash(tmp_path) == ""


def test_git_hash_follows_a_linked_worktree(tmp_path):
    sha = "fedcba9876543210fedcba9876543210fedcba98"
    main, tree = tmp_path / "main" / ".git", tmp_path / "wt"
    (main / "refs" / "heads").mkdir(parents=True)
    (main / "refs" / "heads" / "feat").write_text(sha + "\n")
    admin = main / "worktrees" / "wt"
    admin.mkdir(parents=True)
    (admin / "HEAD").write_text("ref: refs/heads/feat\n")
    (admin / "commondir").write_text("../..\n")
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {admin}\n")
    assert app.git_hash(tree) == "fedcba9"


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


def test_flush_cache_cli_needs_no_keys_and_flushes_by_portfolio_and_period(monkeypatch, tmp_path, capsys):
    from lib.store import PeriodRow, Store

    db = str(tmp_path / "x.db")
    for var in ("TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "XAI_API_KEY", "ZAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DB_PATH", db)
    s = Store(db)
    row = lambda pid, a, b: PeriodRow(pid, a, b, 1.0, 1.0, "AUD", [])  # noqa: E731
    s.period_put([row(70007, "2023-12-26", "2024-01-01"), row(70007, "2024-03-01", "2024-03-31"),
                  row(70007, "2025-01-01", "2025-12-31"), row(80008, "2024-03-01", "2024-03-31")])

    def run(*words):
        with pytest.raises(SystemExit) as e:
            app.main(["--flush-cache", *words])
        return e.value.code, capsys.readouterr().out

    assert run("70007", "2024") == (0, "Flushed 2 cached periods (portfolio 70007, 2024-01-01 to 2024-12-31).\n")
    assert run("2025-06") == (0, "Flushed 1 cached periods (all portfolios, 2025-06-01 to 2025-06-30).\n")
    assert run("80008") == (0, "Flushed 1 cached periods (portfolio 80008).\n")
    assert run() == (0, "Flushed 0 cached periods (all portfolios).\n")
    code, out = run("abc")
    assert code == 1 and "Usage" in out
    assert run("70007", "70008")[0] == 1  # one portfolio at a time


async def test_backfill_task_starts_unless_period_backfill_is_off(env, store, monkeypatch):
    started = []

    async def fake_loop(ctx):
        started.append(ctx)

    monkeypatch.setattr(app.backfill, "loop", fake_loop)
    for extra, expected in (({"PERIOD_BACKFILL": "off"}, 0), ({}, 1), ({"PERIOD_BACKFILL": "x"}, 2)):
        st = config.load({**env, **extra})
        ctx = Ctx(st, store, ScriptedBackend([]), registry(), FakeBot())
        built = app.build_app(ctx)
        await built.post_init(SimpleNamespace(bot=SimpleNamespace(id=1)))
        await asyncio.sleep(0)
        assert len(started) == expected


def test_the_mcp_librarys_unlabelled_protocol_and_ping_lines_are_dropped(monkeypatch):
    import logging

    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    root.handlers.clear()
    try:
        app.setup_logging()
        keep = [h for h in root.handlers]
        mk = lambda msg: logging.LogRecord("mcp.client", logging.INFO, "f", 1, msg, None, None)  # noqa: E731
        assert all(not h.filter(mk("Negotiated protocol version: 2025-11-25")) for h in keep)
        assert all(not h.filter(mk("Unknown SSE event: ping")) for h in keep)
        assert all(h.filter(mk("Unknown SSE event: other")) for h in keep)  # other unknown events still show
        assert all(h.filter(mk("MCP yahoo: connected, protocol 2025-11-25")) for h in keep)
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
