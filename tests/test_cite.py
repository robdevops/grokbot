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


def test_nothing_is_marked_without_snippets():
    assert mark_by_overlap(TEXT, [Source("T", A)]) == TEXT and mark_by_overlap(TEXT, []) == TEXT


def test_pages_the_model_did_not_cite_are_added_after_their_sentences_and_all_are_numbered_in_order():
    from lib.llm.cite import cite_answer
    srcs = [Source("Release", A, PRESS), Source("News", B, NEWS)]
    model = TEXT.replace("EPS $2.09.", 'EPS $2.09.[<a href="https://b.example/">7</a>]')  # it cited News for the figures
    out = cite_answer(model, srcs)
    # the model's own link stays where it put it (number 1, though it wrote 7); Release, which it did not cite, goes
    # after the Data Center sentence it fits (number 2); News is not cited a second time on the Street sentence
    assert out == (
        "AMD's quarter: revenue $13.38b, +45% y/y, EPS $2.09." + LINK_B.replace(">2<", ">1<") + " Data Center did the work, "
        "$8.5b." + LINK_A.replace(">1<", ">2<") + " Street is bullish, with JPMorgan and Morgan Stanley raising "
        "targets to $575 and $550 while staying neutral. My read: the multiple is high.")
    assert cite_answer(TEXT, []) == TEXT


def test_the_models_own_links_are_kept_only_for_pages_the_search_returned_and_renumbered():
    from lib.llm.cite import normalise_markers
    srcs = [Source("A", A), Source("B", B)]
    model = ('One.[<a href="https://b.example">3</a>] Two.[<a href="https://made.up/x">2</a>] '
             'Three.[<a href="https://a.example/x?p=1&amp;q=2">1</a>][<a href="https://b.example/">3</a>]')
    out = normalise_markers(model, srcs)
    # b.example (given without its slash) is 1, the invented page is gone, a.example is 2 and b.example stays 1
    assert out == ('One.[<a href="https://b.example/">1</a>] Two. '
                   'Three.[<a href="https://a.example/x?p=1&amp;q=2">2</a>][<a href="https://b.example/">1</a>]')
    assert normalise_markers(model, []) == "One. Two. Three."  # no search happened: nothing to cite
    assert mark_by_overlap(out, [Source("Release", A, PRESS)]) == out  # the model's markers are enough
