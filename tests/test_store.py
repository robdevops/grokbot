import pytest

from lib.store import Store, is_dm

from .conftest import make_msg


def test_is_dm():
    assert is_dm(5467329077) and not is_dm(-100) and not is_dm(-1001234567890)


def test_group_history_is_shared_between_bots(tmp_path):
    a, b = Store(str(tmp_path / "x.db")), Store(str(tmp_path / "x.db"))
    a.bot_id, b.bot_id = 111, 222
    a.save(make_msg(1, "group hi", chat_id=-100))
    assert [r.text for r in b.history(-100, 20)] == ["group hi"]


def test_dm_history_is_per_bot_and_ids_do_not_collide(tmp_path):
    a, b = Store(str(tmp_path / "x.db")), Store(str(tmp_path / "x.db"))
    a.bot_id, b.bot_id = 111, 222
    me = 5467329077
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
