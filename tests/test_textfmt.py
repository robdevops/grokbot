import re

from tgbot import textfmt as t


def test_plain_text():
    assert t.plain_text('<b>Hi</b> <a href="https://x.com/a?b=1&amp;c=2">link</a> &lt;5%') == \
        "Hi link (https://x.com/a?b=1&c=2) <5%"


def test_md_to_html_and_disclaimer():
    assert t.md_to_html("**bold** `c` [x](https://a.b/c)") == '<b>bold</b> <code>c</code> <a href="https://a.b/c">x</a>'
    assert t.strip_disclaimer("Buy it. Not financial advice.") == "Buy it."


def test_tool_syntax_does_not_eat_the_answer():
    assert t.TOOL_SYNTAX_RE.sub("", "Answer.\n\n<tool_call>junk\n\nMore").strip() == "Answer.\n\nMore"
    assert t.TOOL_SYNTAX_RE.sub("", "A <tool_call><function=x></function></tool_call> B") == "A  B"
    assert t.TOOL_SYNTAX_RE.sub("", "<tool_call><function=web_search></function></tool_call>").strip() == ""


def test_tg_tag_regex_keeps_bare_lt():
    s = lambda x: t.TG_TAG_RE.sub("", x)  # noqa: E731
    assert s("NVDA <5% ok <b>x</b>") == "NVDA <5% ok x"
    assert s('ok <a href="ht') == "ok "


def _balanced(chunk: str) -> bool:
    stack = []
    for m in t.HTML_TAG_RE.finditer(chunk):
        if not m.group(1):
            stack.append(m.group(2).lower())
        elif not stack or stack.pop() != m.group(2).lower():
            return False
    return not stack


def test_split_html_keeps_tags_balanced():
    body = "<b>H</b>\n" + ("word " * 300 + '<a href="https://x.com/q?a=1&amp;b=2">l <b>b &amp; m</b></a> ') * 12
    chunks = t.split_html(body)
    assert len(chunks) > 1 and all(len(c) <= 4096 and _balanced(c) for c in chunks)
    assert not any(re.search(r"<[^>]*$|&\w*$", c) for c in chunks)
    strip = lambda x: re.sub(r"\s+", "", t.plain_text(x))  # noqa: E731
    assert strip("".join(chunks)) == strip(body)


def test_split_html_edge_cases():
    assert t.split_html("short") == ["short"]
    cs = t.split_html("<a href='u'>" + "x" * 9000 + "</a>")
    assert all(len(c) <= 4096 and _balanced(c) for c in cs)
    cs = t.split_html("<5% from ATH " * 500)
    assert all(len(c) <= 4096 for c in cs)
    cs = t.split_html("<b>" * 20 + "x " * 3000 + "</b>" * 20)
    assert len(cs) > 1 and all(len(c) <= 4096 and _balanced(c) for c in cs)
