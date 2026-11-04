from lib.llm.base import Source
from lib.llm.cite import mark_by_overlap, mark_citations

A, B = "https://a.example/x?p=1&q=2", "https://b.example/"
LINK_A = '[<a href="https://a.example/x?p=1&amp;q=2">1</a>]'
LINK_B = '[<a href="https://b.example/">2</a>]'


def test_citations_become_numbered_links_after_the_sentence():
    text = "NVDA rose 3%. Micron guided up. Both beat."
    out = mark_citations(text, [(12, A), (30, B), (41, A)])
    assert out == f"NVDA rose 3%.{LINK_A} Micron guided up.{LINK_B} Both beat.{LINK_A}"
    assert mark_citations("One.", [(3, A), (3, B)]) == f"One.{LINK_A}{LINK_B.replace('2', '2')}"
    assert mark_citations('See <a href="u">x</a>.', [(10, A)]) == 'See <a href="u">x</a>.'
    assert mark_citations("Hi.", [(99, A)]).startswith("Hi.[") and mark_citations("Hi.", [(0, A)]) == "Hi."


PRESS = ("AMD reported third quarter revenue of $13.38 billion, up 45% year over year, with Data Center revenue of "
         "$8.5 billion and non-GAAP earnings per share of $2.09. Gross margin was 56%.")
NEWS = ("AMD shares fell after hours although analysts at Morgan Stanley raised their price target to $550 and "
        "JPMorgan raised its target to $575, staying neutral on the stock.")
TEXT = ("AMD's quarter: revenue $13.38b, +45% y/y, EPS $2.09. Data Center did the work, $8.5b. "
        "Street is bullish, with JPMorgan and Morgan Stanley raising targets to $575 and $550 while staying neutral. "
        "My read: the multiple is high.")


def test_each_source_is_cited_after_the_sentence_it_fits_best():
    out = mark_by_overlap(TEXT, [Source("Release", A, PRESS), Source("News", B, NEWS)])
    assert "EPS $2.09." + LINK_A in out and "$550 while staying neutral." + LINK_B in out
    assert out.endswith("My read: the multiple is high.")  # an opinion fits no source: no marker


def test_numbering_follows_the_order_in_the_text_and_a_source_that_fits_nothing_gets_no_marker():
    out = mark_by_overlap(TEXT, [Source("News", B, NEWS), Source("Release", A, PRESS),
                                 Source("Other", "https://c.example/", "Completely unrelated gardening advice.")])
    assert "EPS $2.09.[<a href=\"https://a.example/x?p=1&amp;q=2\">1</a>]" in out and 'c.example' not in out
    assert out.count("</a>]") == 2


def test_nothing_is_marked_without_snippets_or_when_markers_exist_already():
    assert mark_by_overlap(TEXT, [Source("T", A)]) == TEXT and mark_by_overlap(TEXT, []) == TEXT
    marked = mark_by_overlap(TEXT, [Source("Release", A, PRESS)])
    assert mark_by_overlap(marked, [Source("News", B, NEWS)]) == marked
