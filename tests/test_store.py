import pytest

from lib.store import Store, is_dm

from .conftest import make_msg


def test_is_dm():
    assert is_dm(1234567890) and not is_dm(-100) and not is_dm(-1001234567890)


def test_group_history_is_shared_between_bots(tmp_path):
    a, b = Store(str(tmp_path / "x.db")), Store(str(tmp_path / "x.db"))
    a.bot_id, b.bot_id = 111, 222
    a.save(make_msg(1, "group hi", chat_id=-100))
    assert [r.text for r in b.history(-100, 20)] == ["group hi"]


def test_dm_history_is_per_bot_and_ids_do_not_collide(tmp_path):
    a, b = Store(str(tmp_path / "x.db")), Store(str(tmp_path / "x.db"))
    a.bot_id, b.bot_id = 111, 222
    me = 1234567890
    a.save(make_msg(50, "news please", chat_id=me, ts=1))
    a.save(make_msg(51, "Fresh headlines", sender="BotA", chat_id=me, ts=2))
    b.save(make_msg(10, "zeitgeist?", chat_id=me, ts=3))
    a.save(make_msg(7, "from A", chat_id=me, ts=4))
    b.save(make_msg(7, "from B", chat_id=me, ts=5))
    assert [r.text for r in b.history(me, 20)] == ["from B", "zeitgeist?"]
    assert [r.text for r in a.history(me, 20)] == ["from A", "news please", "Fresh headlines"]
    assert a.dm_chats() == [me]


def test_dm_before_bot_id_fails_clearly(tmp_path):
    s = Store(str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError, match="bot id"):
        s.save(make_msg(1, "x", chat_id=5))
    s.save(make_msg(1, "group fine", chat_id=-5))  # groups need no bot id


def test_history_window_is_stepped(store):
    for i in range(1, 46):
        store.save(make_msg(i, f"m{i}", chat_id=-1, ts=i))
    rows = store.history(-1, 20)
    assert len(rows) == 25 and rows[0].message_id == 21 and rows[-1].message_id == 45
    # the start only moves every limit/2 messages
    starts = set()
    for i in range(46, 66):
        store.save(make_msg(i, f"m{i}", chat_id=-1, ts=i))
        rows = store.history(-1, 20)
        assert 20 <= len(rows) <= 29
        starts.add(rows[0].message_id)
    assert len(starts) <= 3


def test_kv_users_news_and_usage(store):
    assert store.kv_get("k") is None
    store.kv_set("k", "v")
    assert store.kv_get("k") == "v"
    store.save(make_msg(1, "x", chat_id=-1))
    store.remember_user(make_msg(2, "x", username="Bob", chat_id=-1))
    assert store.user_id_for("bob") == 5
    store.set_muted("bob", "NVDA", True)
    store.set_muted("bob", "AMD", True)
    store.set_muted("bob", "AMD", False)
    assert store.muted_codes("bob") == {"NVDA"}
    store.remember_news_message(9, 3, ["A", "B"])
    assert store.news_message_codes(9, 3) == ["A", "B"] and store.news_message_codes(9, 4) == []
    store.add_news("bob", "• A: thing happened")
    assert store.recent_news("bob", 0) == ["• A: thing happened"]
    store.add_usage(-1, "m", "chat", 2, 1000, 400, 50, 0.01)
    assert store.usage_summary(0)[0][:5] == ("m", 1, 1000, 400, 50)


def test_writes_are_committed_immediately(tmp_path):
    path = str(tmp_path / "shared.db")
    first = Store(path)
    first.bot_id = 7
    first.kv_set("k", "v")
    first.remember_chat(-1, "Group")
    second = Store(path)  # another process sharing the file sees the writes without any explicit commit
    second.bot_id = 7
    assert second.kv_get("k") == "v" and second.chats() == [(-1, "Group")]


def test_period_cache_round_trip_flush_and_version(tmp_path):
    from lib.store import PeriodRow

    path = str(tmp_path / "p.db")
    s = Store(path)
    s.period_put([PeriodRow(7, "2026-01-01", "2026-01-31", 1.5, 150.0, "AUD", [["AAA", 90.0, 12.0]]),
                  PeriodRow(7, "2026-02-01", "2026-02-28", -2.0, -200.0, "AUD", []),
                  PeriodRow(8, "2026-01-01", "2026-01-31", 3.0, 30.0, "USD", [])])
    got = s.period_get(7, "2026-01-01", "2026-02-15")  # only windows lying within the range
    assert list(got) == [("2026-01-01", "2026-01-31")] and got[("2026-01-01", "2026-01-31")].movers == [["AAA", 90.0, 12.0]]
    assert s.period_count(7, "2000-01-01", "2100-01-01") == 2
    s.period_put([PeriodRow(7, "2026-01-01", "2026-01-31", 9.0, 1.0, "AUD", [])])  # replaces
    assert s.period_get(7, "2026-01-01", "2026-01-31")[("2026-01-01", "2026-01-31")].pct == 9.0
    s._conn.execute("UPDATE period_cache SET v = 0 WHERE portfolio_id = 8")  # an older format
    assert Store(path).period_count(8, "2000-01-01", "2100-01-01") == 0  # dropped on open
    assert s.period_flush(7) == 2 and s.period_flush() == 0
