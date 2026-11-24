"""Turn gemtext, the plain-text page format of Gemini, into safe HTML."""

import html
import re
from urllib.parse import quote, urlsplit

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from gemini_browser.gemini_client import resolve_link

_LINK_LINE = re.compile(r"^=>[ \t]*(\S+)(?:[ \t]+(.*))?$")
_HEADING_LINE = re.compile(r"^(#{1,3})(?!#)[ \t]*(.*)$")
_LIST_LINE = re.compile(r"^\*[ \t]+(.*)$")
_QUOTE_LINE = re.compile(r"^>[ \t]?(.*)$")
_WEB_SCHEMES = ("http", "https")


class RenderedPage(FrozenModel):
    """A gemtext page as HTML, with the title the page gives itself."""

    html: str = Field(
        description="The page body as HTML; every piece of page text is escaped"
    )
    title: str = Field(
        description="The first heading on the page, or empty when it has none"
    )


def page_link(address: str) -> str:
    """The app address that opens a gemini:// address in this browser."""
    return f"/?url={quote(address, safe='')}"


def _link_html(base_url: str, target: str, label: str) -> str:
    resolved = resolve_link(base_url, target)
    scheme = urlsplit(resolved).scheme.lower()
    shown = html.escape(label or resolved)
    host = urlsplit(resolved).hostname or ""
    page_host = urlsplit(base_url).hostname or ""
    host_note = (
        f'<span class="host">{html.escape(host)}</span>'
        if host and host != page_host
        else ""
    )
    if scheme == "gemini":
        href = html.escape(page_link(resolved), quote=True)
        return f'<a class="link" href="{href}" title="{html.escape(resolved, quote=True)}"><span class="arrow">=&gt;</span><span class="label">{shown}</span>{host_note}</a>'
    if scheme in _WEB_SCHEMES:
        href = html.escape(resolved, quote=True)
        return (
            f'<a class="link" href="{href}" target="_blank" rel="noopener noreferrer" title="{href}">'
            f'<span class="arrow">=&gt;</span><span class="label">{shown}</span>'
            f'<span class="host">{html.escape(host)} (web page)</span></a>'
        )
    # Any other scheme (mailto:, javascript:, ...) is shown as text and never made clickable.
    return (
        f'<span class="link plain"><span class="arrow">=&gt;</span><span class="label">{shown}</span>'
        f'<span class="host">{html.escape(resolved)}</span></span>'
    )


def render_gemtext(text: str, base_url: str) -> RenderedPage:
    """Render gemtext lines as headings, links, lists, quotes, preformatted blocks and paragraphs."""
    parts: list[str] = []
    title = ""
    in_preformatted = False
    preformatted_lines: list[str] = []
    preformatted_label = ""
    list_items: list[str] = []
    quote_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("```"):
            if in_preformatted:
                parts.append(_preformatted_html(preformatted_label, preformatted_lines))
                preformatted_lines = []
                in_preformatted = False
            else:
                _flush(parts, list_items, quote_lines)
                list_items, quote_lines = [], []
                in_preformatted = True
                preformatted_label = line[3:].strip()
            continue
        if in_preformatted:
            preformatted_lines.append(line)
            continue

        list_match = _LIST_LINE.match(line)
        quote_match = _QUOTE_LINE.match(line)
        if list_match is None and list_items:
            parts.append(_list_html(list_items))
            list_items = []
        if quote_match is None and quote_lines:
            parts.append(_quote_html(quote_lines))
            quote_lines = []

        link_match = _LINK_LINE.match(line)
        heading_match = _HEADING_LINE.match(line)
        if link_match is not None:
            parts.append(
                _link_html(
                    base_url, link_match.group(1), (link_match.group(2) or "").strip()
                )
            )
        elif heading_match is not None:
            level = len(heading_match.group(1))
            heading = heading_match.group(2).strip()
            title = title or (heading if level == 1 else "")
            parts.append(f"<h{level}>{html.escape(heading)}</h{level}>")
        elif list_match is not None:
            list_items.append(html.escape(list_match.group(1)))
        elif quote_match is not None:
            quote_lines.append(html.escape(quote_match.group(1)))
        elif line.strip():
            parts.append(f"<p>{html.escape(line)}</p>")

    if in_preformatted:
        parts.append(_preformatted_html(preformatted_label, preformatted_lines))
    _flush(parts, list_items, quote_lines)
    return RenderedPage(html="".join(parts), title=title)


def _preformatted_html(label: str, lines: list[str]) -> str:
    aria = f' aria-label="{html.escape(label, quote=True)}"' if label else ""
    return f"<pre{aria}>{html.escape(chr(10).join(lines))}</pre>"


def _list_html(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _quote_html(lines: list[str]) -> str:
    return (
        "<blockquote>" + "".join(f"<p>{line}</p>" for line in lines) + "</blockquote>"
    )


def _flush(parts: list[str], list_items: list[str], quote_lines: list[str]) -> None:
    if list_items:
        parts.append(_list_html(list_items))
    if quote_lines:
        parts.append(_quote_html(quote_lines))
