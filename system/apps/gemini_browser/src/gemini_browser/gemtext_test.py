import pytest

from gemini_browser.gemtext import page_link, render_gemtext

BASE = "gemini://example.org/dir/page.gmi"


def render(text: str) -> str:
    return render_gemtext(text, BASE).html


def test_headings_use_their_level_and_the_first_top_level_one_is_the_title() -> None:
    page = render_gemtext("## Section\n# First\n### Small\n# Second\n", BASE)
    assert page.html == "<h2>Section</h2><h1>First</h1><h3>Small</h3><h1>Second</h1>"
    assert page.title == "First"


def test_a_page_without_a_top_level_heading_has_no_title() -> None:
    assert render_gemtext("## Only a section\ntext", BASE).title == ""


def test_four_hashes_are_a_paragraph_not_a_heading() -> None:
    assert render("#### deep") == "<p>#### deep</p>"


def test_blank_lines_are_dropped_and_text_lines_become_paragraphs() -> None:
    assert render("one\n\n   \ntwo\r\n") == "<p>one</p><p>two</p>"


def test_consecutive_list_lines_share_one_list_and_a_break_starts_another() -> None:
    assert (
        render("* a\n* b\ntext\n* c")
        == "<ul><li>a</li><li>b</li></ul><p>text</p><ul><li>c</li></ul>"
    )


def test_a_star_without_a_space_is_a_paragraph() -> None:
    assert render("*bold*") == "<p>*bold*</p>"


def test_consecutive_quote_lines_share_one_quote_and_with_or_without_a_space() -> None:
    assert render("> one\n>two\n>\nplain") == (
        "<blockquote><p>one</p><p>two</p><p></p></blockquote><p>plain</p>"
    )


def test_a_list_directly_followed_by_a_quote_closes_the_list_first() -> None:
    assert (
        render("* item\n> said")
        == "<ul><li>item</li></ul><blockquote><p>said</p></blockquote>"
    )


def test_a_quote_directly_followed_by_a_list_closes_the_quote_first() -> None:
    assert (
        render("> said\n* item")
        == "<blockquote><p>said</p></blockquote><ul><li>item</li></ul>"
    )


def test_a_list_still_open_at_the_end_is_closed() -> None:
    assert render("* last") == "<ul><li>last</li></ul>"


def test_a_link_line_in_the_middle_of_a_list_ends_the_list() -> None:
    html = render("* a\n=> /x label\n* b")
    assert html.count("<ul>") == 2
    assert html.index("</ul>") < html.index('class="link"')


def test_preformatted_block_keeps_lines_verbatim_and_does_not_interpret_them() -> None:
    html = render(
        "```ascii art\n# not a heading\n=> /not-a-link\n  <b>  indented\n```\nafter"
    )
    assert html == (
        '<pre aria-label="ascii art"># not a heading\n=&gt; /not-a-link\n  &lt;b&gt;  indented</pre><p>after</p>'
    )


def test_an_unterminated_preformatted_block_runs_to_the_end_and_keeps_its_label() -> (
    None
):
    assert (
        render("```code\nline one\nline two")
        == '<pre aria-label="code">line one\nline two</pre>'
    )


def test_a_preformatted_block_without_alt_text_has_no_label() -> None:
    assert render("```\nx\n```") == "<pre>x</pre>"


def test_alt_text_cannot_break_out_of_its_attribute() -> None:
    html = render('```" onmouseover="alert(1)\nx\n```')
    assert 'aria-label="&quot; onmouseover=&quot;alert(1)"' in html
    assert '" onmouseover' not in html


def test_a_preformatted_block_closes_an_open_list_and_quote_before_it() -> None:
    assert render("* a\n```\nx\n```").startswith("<ul><li>a</li></ul><pre>")
    assert render("> q\n```\nx\n```").startswith(
        "<blockquote><p>q</p></blockquote><pre>"
    )


def test_every_kind_of_page_text_is_escaped() -> None:
    attack = "<script>alert(1)</script>"
    html = render(
        f"{attack}\n# {attack}\n* {attack}\n> {attack}\n=> /x {attack}\n```\n{attack}\n```"
    )
    assert "<script" not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 6


def test_a_relative_link_resolves_against_the_page_and_opens_in_this_browser() -> None:
    html = render("=> other.gmi The other page")
    target = "gemini://example.org/dir/other.gmi"
    assert f'href="{page_link(target)}"' in html
    assert f'title="{target}"' in html
    assert '<span class="label">The other page</span>' in html
    assert "target=" not in html


def test_an_absolute_path_link_resolves_from_the_site_root() -> None:
    assert page_link("gemini://example.org/top.gmi") in render("=> /top.gmi")


def test_a_link_without_a_label_shows_its_resolved_address() -> None:
    assert '<span class="label">gemini://example.org/dir/other.gmi</span>' in render(
        "=> other.gmi"
    )


def test_a_link_to_another_host_names_that_host_and_one_to_the_same_host_does_not() -> (
    None
):
    other = render("=> gemini://elsewhere.net/ Away")
    assert '<span class="host">elsewhere.net</span>' in other
    assert 'class="host"' not in render("=> /here Home")


def test_a_gemini_link_is_percent_encoded_into_the_browsers_address_parameter() -> None:
    assert (
        page_link("gemini://a.b/p?q=1 2") == "/?url=gemini%3A%2F%2Fa.b%2Fp%3Fq%3D1%202"
    )


def test_web_links_open_in_a_new_window_without_giving_the_page_access() -> None:
    html = render("=> https://example.com/x A web page")
    assert 'href="https://example.com/x"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "example.com (web page)" in html


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "mailto:a@b.c",
        "data:text/html,<b>x</b>",
        "ftp://example.org/f",
        "file:///etc/passwd",
    ],
)
def test_links_of_other_kinds_are_shown_as_text_and_never_clickable(
    target: str,
) -> None:
    html = render(f"=> {target} label")
    assert "<a " not in html
    assert "href=" not in html
    assert '<span class="link plain">' in html
    assert '<span class="label">label</span>' in html


def test_the_label_of_a_link_cannot_inject_markup() -> None:
    html = render('=> /x <img src=x onerror="alert(1)">')
    assert "<img" not in html
    assert "&lt;img" in html


def test_an_address_with_quotes_cannot_break_out_of_the_href() -> None:
    html = render('=> gemini://example.org/a"onclick="alert(1) label')
    assert '"onclick' not in html
    assert " onclick=" not in html


def test_link_lines_accept_tabs_and_extra_spaces_around_the_address_and_label() -> None:
    html = render("=>\t/x \t Spaced label  ")
    assert '<span class="label">Spaced label</span>' in html


def test_an_empty_input_renders_nothing() -> None:
    page = render_gemtext("", BASE)
    assert page.html == ""
    assert page.title == ""
