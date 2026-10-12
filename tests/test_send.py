import pytest
from telegram.error import BadRequest, TelegramError

from lib.telegram.send import CAPTION_MAX, reply_with_photos

from .conftest import FakeBot, user_msg

PNG = b"\x89PNG\r\n\x1a\nfake"


async def reply(bot, store, text, images, **kw):
    msg = user_msg(bot, "chart please", mid=5)
    await reply_with_photos(msg, images, text, store, **kw)
    return msg


async def test_a_short_answer_is_the_caption_of_one_photo(store):
    bot = FakeBot()
    msg = await reply(bot, store, "Semis are <b>52%</b>.", [PNG])
    assert len(msg.photo_replies) == 1 and msg.replies == []
    sent = msg.photo_replies[0]
    assert sent["photo"] == PNG and sent["caption"] == "Semis are <b>52%</b>." and sent["do_quote"] is True
    assert sent["disable_notification"] is True
    assert [r.text for r in store.history(-100, 20)] == ["Semis are <b>52%</b>."]  # saved, so follow-ups see it


async def test_several_charts_go_as_one_album_with_the_caption_on_the_first(store):
    bot = FakeBot()
    msg = await reply(bot, store, "Three portfolios.", [PNG, PNG, PNG])
    assert len(msg.photo_replies) == 1 and msg.replies == []
    album = msg.photo_replies[0]["album"]
    assert [m.caption for m in album] == ["Three portfolios.", None, None]


async def test_more_than_ten_charts_split_into_albums_and_only_the_first_has_the_caption(store):
    bot = FakeBot()
    msg = await reply(bot, store, "Many.", [PNG] * 12)
    first, second = msg.photo_replies
    assert len(first["album"]) == 10 and first["album"][0].caption == "Many." and len(second["album"]) == 2
    assert second["album"][0].caption is None and second["do_quote"] is False


async def test_an_answer_too_long_for_a_caption_follows_as_text(store):
    bot = FakeBot()
    long = "word " * (CAPTION_MAX // 5 + 20)
    msg = await reply(bot, store, long, [PNG])
    assert msg.photo_replies[0]["caption"] is None  # the picture alone
    assert msg.replies and "word" in msg.replies[0]["text"]  # then the answer as a normal message


async def test_a_rejected_caption_is_sent_as_plain_text(store):
    bot = FakeBot()
    msg = user_msg(bot, "chart", mid=5)
    real = msg.reply_photo
    calls = []

    async def picky(photo, **kw):
        calls.append(kw.get("parse_mode"))
        if kw.get("parse_mode"):
            raise BadRequest("Can't parse entities: bad tag")
        return await real(photo, **kw)

    msg.reply_photo = picky
    await reply_with_photos(msg, [PNG], "Bad <tag>", store)
    assert len(calls) == 2 and calls[1] is None and msg.photo_replies[0]["caption"] == "Bad "


async def test_if_telegram_refuses_the_picture_the_answer_is_still_sent(store):
    bot = FakeBot()
    msg = user_msg(bot, "chart", mid=5)

    async def refuse(photo, **kw):
        raise TelegramError("Bad Request: not enough rights to send photos")

    msg.reply_photo = refuse
    await reply_with_photos(msg, [PNG], "The answer.", store)
    assert msg.photo_replies == [] and [r["text"] for r in msg.replies] == ["The answer."]


async def test_other_bad_requests_are_not_swallowed_as_parse_errors(store):
    bot = FakeBot()
    msg = user_msg(bot, "chart", mid=5)

    async def broken(photo, **kw):
        raise BadRequest("Wrong file identifier")

    msg.reply_photo = broken
    await reply_with_photos(msg, [PNG], "Answer.", store)  # falls back to text instead of crashing
    assert [r["text"] for r in msg.replies] == ["Answer."]


@pytest.mark.parametrize("n", [1, 3])
async def test_the_markup_rides_on_a_single_photo_only(store, n):
    bot = FakeBot()
    msg = await reply(bot, store, "Hi.", [PNG] * n, last_markup="KB")
    assert (msg.photo_replies[0].get("reply_markup") == "KB") is (n == 1)
