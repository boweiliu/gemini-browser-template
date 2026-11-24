"""Browse any gemini:// address: type a URL, read the page, follow links, answer input prompts, and see the original page source..

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/gemini-browser/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/gemini-browser/`` but
  honors the ``GEMINI_BROWSER_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``GEMINI_BROWSER_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://gemini-browser.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import html
import os
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.request_guard import install_request_guard
from app_schema.routes import model_response
from flask import Flask, Response, abort, redirect, request, send_file
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple
from werkzeug.wrappers import Response as WerkzeugResponse

from workspace_layout.identity import (
    IDENTITY_HEADER,
    RequestIdentity,
    is_owner_participant,
    parse_identity_header,
    participant_key_of,
)
from workspace_layout.errors import InvalidLayoutValueError

from gemini_browser.gemini_client import (
    REDIRECT_STATUSES,
    FetchResult,
    fetch,
    normalize_address,
    replace_query,
    resolve_link,
    without_query,
)
from gemini_browser.gemini_errors import CertificateChangedError, GeminiError
from gemini_browser.gemini_store import (
    CertificateCheck,
    recent_visits,
    record_visit,
    trust_host,
    verify_host,
)
from gemini_browser.gemtext import page_link, render_gemtext

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/gemini-browser/`` but is overridable via the ``GEMINI_BROWSER_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/gemini-browser/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("GEMINI_BROWSER_DATA_DIR", "data/.apps/gemini-browser"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``GEMINI_BROWSER_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("GEMINI_BROWSER_PORT", "8081"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path(
    "system/apps/system_interface/imbue/system_interface/static/_static"
)
SHELL_STATIC_MODULE_NAMES = ("app_contract.js", "context_menu.js")

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return (
        "'sha256-"
        + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        + "'"
    )


# Every HTML page is served under this policy (the app is reachable from the
# internet once the workspace is shared): scripts only from this origin plus the
# inline blocks listed here by hash, fetches, sockets and images only from this
# origin, inline styles, frames of any http(s) page, no plugins. Anything else a
# page pulls in (a CDN script, Google Fonts, an image hosted elsewhere, a
# third-party API called from the browser) is blocked until its origin is added
# to the matching directive; a new inline <script> needs its
# ``inline_script_source`` added.
# The browser posts every block to ``/_csp-report`` below, which logs it on
# stderr (the supervisor log), so a blocked resource is never silent; the
# ``'report-sample'`` makes the browser include where a blocked inline script
# is and how it starts, since the report otherwise names it only as "inline".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' {inline_script_source(SHELL_PAGE_SCRIPT_BODY)}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = (
    "chrome-extension:",
    "moz-extension:",
    "safari-web-extension:",
)


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="a Gemini page, or the start page, as HTML"),
    ("POST", "/"): SchemalessRoute(
        reason="a Gemini page loaded with a secret answer that must stay out of the address"
    ),
    ("GET", "/download"): SchemalessRoute(reason="a file sent by a Gemini server"),
    ("POST", "/trust"): SchemalessRoute(
        reason="a form post that saves a certificate and redirects back to the page"
    ),
    ("GET", "/app.js"): SchemalessRoute(
        reason="the script behind the back and forward buttons"
    ),
    ("GET", "/_static/{basename}"): SchemalessRoute(
        reason="a browser-side module the shell builds"
    ),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("POST", "/_csp-report"): SchemalessRoute(
        reason="the browser's report of what the page's policy blocked"
    ),
}

app = Flask("gemini_browser", static_folder=None)
# Ahead of every route: refuse a page of another origin and a header-less request that did not come from loopback.
install_request_guard(app, exempt_rules=frozenset())


# Everyone the workspace admits can browse, each with a profile of their own: the owner's is the app's data
# directory itself (where it has always been), every other requester's is a directory of their own beneath it,
# keyed by who the identity header says they are. Visitors may open public sites only; the owner may open any.
VISITOR_PROFILES_DIR = "visitors"
# Sites the owner vouches for, which a visitor may open even when the name resolves inside the workspace (the
# owner's own capsule, say): comma-separated host names in this environment variable, none by default.
VISITOR_HOSTS_ENV = "GEMINI_BROWSER_VISITOR_HOSTS"


def parse_visitor_hosts(text: str) -> frozenset[str]:
    """The host names in a comma-separated list, lower case and without blanks."""
    return frozenset(name.strip().lower() for name in text.split(",") if name.strip())


VISITOR_HOSTS = parse_visitor_hosts(os.environ.get(VISITOR_HOSTS_ENV, ""))


@app.errorhandler(InvalidLayoutValueError)
def refuse_unusable_identity(error: InvalidLayoutValueError) -> Response:
    """Answer 400 when the identity header names an account id the workspace could not keep files under."""
    logger.error("{} {} refused its identity: {}", request.method, request.path, error)
    return Response("The request's identity could not be used.", status=400, mimetype="text/plain")


def _identity() -> RequestIdentity:
    return parse_identity_header(request.headers.get(IDENTITY_HEADER))


def profile_dir() -> Path:
    """Where the requester's own visited pages and saved certificates live."""
    identity = _identity()
    if is_owner_participant(identity):
        return DATA_DIR
    key = str(participant_key_of(identity))
    label = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:48]
    return DATA_DIR / VISITOR_PROFILES_DIR / f"{label}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def may_open_private_addresses() -> bool:
    """Whether the requester may browse to addresses inside private networks: the owner only."""
    return is_owner_participant(_identity())


# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app,
    DEFAULT_API_SCHEMA_PATH,
    title="Gemini browser",
    description="Browse any gemini:// address: type a URL, read the page, follow links, answer input prompts, and see the original page source.",
    route_table=ROUTE_TABLE,
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


STYLE = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; color: #1d2b2c; background: #fbf8f1; }
  header { display: flex; gap: 8px; align-items: center; padding: 10px 14px; border-bottom: 1px solid #ddd6c5; background: #f3eee1; position: sticky; top: 0; z-index: 2; }
  header .nav { display: flex; gap: 4px; }
  header .btn { width: 32px; height: 32px; display: grid; place-items: center; border: 0; background: none; cursor: pointer; border-radius: 8px; color: #0b292b; text-decoration: none; font: 18px ui-sans-serif, system-ui, sans-serif; }
  header .btn:hover { background: #e6dfcd; }
  header .btn:disabled { color: #b9b19c; cursor: default; background: none; }
  header form { flex: 1; display: flex; }
  header input[name=url] { flex: 1; font: 15px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; padding: 7px 12px; border: 1px solid #cfc7b3; border-radius: 8px; background: #fff; color: #0b292b; }
  header button.go { margin-left: 6px; padding: 0 14px; border: 0; border-radius: 8px; background: #0b292b; color: #f5d6a0; font-size: 14px; cursor: pointer; }
  main { max-width: 720px; margin: 0 auto; padding: 28px 20px 90px; line-height: 1.6; font-size: 16px; overflow-wrap: anywhere; }
  main h1 { font-size: 28px; margin: 0.2em 0 0.5em; }
  main h2 { font-size: 21px; margin: 1.6em 0 0.4em; }
  main h3 { font-size: 17px; margin: 1.4em 0 0.3em; }
  main p { margin: 0.6em 0; }
  main ul { margin: 0.6em 0; padding-left: 1.3em; }
  main blockquote { margin: 0.8em 0; padding: 2px 14px; border-left: 3px solid #cecd0c; color: #4b4c08; background: #f6f3dd; }
  main blockquote p { margin: 0.4em 0; }
  main pre { background: #0b292b; color: #f5d6a0; padding: 12px 14px; border-radius: 8px; overflow-x: auto; font: 14px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  main pre.plain { background: #fff; color: #1d2b2c; border: 1px solid #ddd6c5; white-space: pre-wrap; }
  main img.picture { max-width: 100%; height: auto; border-radius: 8px; }
  main .link { display: flex; gap: 8px; align-items: baseline; padding: 2px 0; color: #0b5d63; text-decoration: none; }
  main a.link:hover .label { text-decoration: underline; }
  main .link .arrow { color: #97630c; flex: none; }
  main .link.plain { color: #6d6753; }
  main .link .host { color: #8a8470; font-size: 13px; }
  main .note { color: #6d6753; font-size: 14px; }
  footer { position: fixed; left: 0; right: 0; bottom: 0; display: flex; gap: 16px; align-items: center; padding: 6px 14px; font-size: 13px; color: #6d6753; background: #f3eee1; border-top: 1px solid #ddd6c5; z-index: 2; }
  footer .grow { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  footer a { color: #0b5d63; }
  details.source { position: fixed; right: 14px; bottom: 34px; width: min(560px, calc(100% - 28px)); z-index: 1; }
  details.source summary { cursor: pointer; text-align: right; font-size: 13px; color: #0b5d63; list-style: none; }
  details.source pre { background: #fff; color: #1d2b2c; border: 1px solid #cfc7b3; max-height: 40vh; overflow: auto; margin-top: 6px; padding: 10px 12px; font: 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; border-radius: 8px; }
  .prompt, .notice { max-width: 520px; margin: 40px auto 0; padding: 20px; border: 1px solid #ddd6c5; border-radius: 12px; background: #fff; }
  .prompt form { display: flex; gap: 8px; margin-top: 12px; }
  .prompt input[name=input] { flex: 1; padding: 8px 10px; border: 1px solid #cfc7b3; border-radius: 8px; font-size: 15px; }
  .prompt button, .notice button { padding: 8px 16px; border: 0; border-radius: 8px; background: #0b292b; color: #f5d6a0; cursor: pointer; font-size: 14px; }
  .notice h2 { margin: 0 0 6px; font-size: 18px; }
  .notice .why { color: #6d6753; margin: 0 0 14px; }
  .notice .fingerprints { font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: #6d6753; margin: 0 0 14px; }
  .notice .actions { display: flex; gap: 14px; align-items: center; font-size: 14px; }
  .notice a { color: #0b5d63; }
  .empty { max-width: 520px; margin: 70px auto 0; }
  .empty h1 { margin-bottom: 4px; }
  .empty .hint { color: #6d6753; margin-top: 0; }
  .empty .recent { margin-top: 28px; }
  .empty .recent h3 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: #8a8470; margin: 0 0 6px; }
  @media (pointer: coarse) {
    header { gap: 4px; padding: 8px; }
    header .nav { gap: 0; }
    header form { min-width: 0; }
    header .btn { width: 44px; height: 44px; }
    header input[name=url] { min-width: 0; font-size: 16px; padding: 11px 10px; }
    header button.go { min-height: 44px; padding: 0 12px; }
    main a.link { padding: 10px 0; }
    details.source summary { padding: 14px 6px; }
    .prompt input[name=input] { min-width: 0; font-size: 16px; padding: 12px 10px; }
    .prompt button, .notice button { min-height: 44px; }
    .notice .actions a, footer a { display: inline-block; padding: 12px 0; }
    footer { padding: 0 14px; }
  }
"""

# The page's only script: the back and forward buttons walk this window's own history.
APP_SCRIPT = """
const back = document.getElementById("back");
const forward = document.getElementById("forward");
back.addEventListener("click", () => history.back());
forward.addEventListener("click", () => history.forward());
if (window.navigation) {
  const sync = () => {
    back.disabled = !window.navigation.canGoBack;
    forward.disabled = !window.navigation.canGoForward;
  };
  sync();
  window.navigation.addEventListener("currententrychange", sync);
}
"""

STARTING_POINTS = (
    ("Project Gemini", "gemini://geminiprotocol.net/"),
    ("Kennedy search engine", "gemini://kennedy.gemi.dev/"),
)
RECENT_VISIT_COUNT = 8
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml")
HOST_KEY_PATTERN = re.compile(r"^[^\s/]+:\d{1,5}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")

FAILURES: dict[int, tuple[str, str]] = {
    40: ("The server had a temporary problem", "Try again in a moment."),
    41: (
        "The server is unavailable",
        "It may be overloaded or down for maintenance. Try again later.",
    ),
    42: (
        "The server's program failed",
        "The page is generated by a program on the server, and it errored.",
    ),
    43: (
        "A gateway failed",
        "The server could not reach the other service it needed for this page.",
    ),
    44: (
        "The server asks you to slow down",
        "You made requests too quickly. Wait a little before trying again.",
    ),
    50: (
        "The server cannot show this page",
        "The server says this will not work later either.",
    ),
    51: (
        "This page was not found",
        "Check the address, or go up to the site's home page.",
    ),
    52: (
        "This page is gone",
        "The server says it was removed on purpose and will not return.",
    ),
    53: ("The server will not fetch other sites", "It only serves its own pages."),
    59: ("The server did not understand the request", "The address may be malformed."),
    60: (
        "This page needs a client certificate",
        "That is a kind of sign-in this browser does not support yet.",
    ),
    61: (
        "Your certificate is not accepted here",
        "This browser does not support client certificates yet.",
    ),
    62: (
        "Your certificate is not valid",
        "This browser does not support client certificates yet.",
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _verify_certificate(host_key: str, fingerprint: str) -> CertificateCheck:
    return verify_host(profile_dir(), host_key, fingerprint, _now())


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _format_date(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).strftime("%d %b %Y").lstrip("0")


def _certificate_note(check: CertificateCheck) -> str:
    if check.is_new:
        return "first visit, certificate saved"
    return f"certificate trusted since {_format_date(check.first_seen)}"


def _page(
    *,
    title: str,
    address: str,
    main: str,
    status: str,
    source: str = "",
    actions: str = "",
) -> Response:
    """One full page: the address bar, the content, the source panel and the status line."""
    reload_href = page_link(address) if address else "/"
    document = (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{_escape(title)}</title>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'><style>{STYLE}</style></head><body>"
        '<header><div class="nav">'
        '<button class="btn" id="back" type="button" title="Back">&larr;</button>'
        '<button class="btn" id="forward" type="button" title="Forward">&rarr;</button>'
        f'<a class="btn" id="reload" href="{_escape(reload_href)}" title="Reload">&#8635;</a></div>'
        f'<form action="/" method="get"><input name="url" value="{_escape(address)}" placeholder="gemini://" '
        'spellcheck="false" autocomplete="off" aria-label="Gemini address"><button class="go">Go</button></form></header>'
        f"<main>{main}</main>{source}"
        f'<footer><span class="grow">{status}</span>{actions}</footer>'
        '<script src="/app.js" defer></script>' + SHELL_PAGE_SCRIPT + "</body></html>"
    )
    return Response(document, mimetype="text/html")


def _source_panel(text: str) -> str:
    return f'<details class="source"><summary>View source</summary><pre>{html.escape(text)}</pre></details>'


def _notice(
    *, title: str, address: str, heading: str, why: str, actions: str, status: str
) -> Response:
    main = f'<div class="notice"><h2>{_escape(heading)}</h2><p class="why">{_escape(why)}</p>{actions}</div>'
    return _page(title=title, address=address, main=main, status=_escape(status))


def _retry_actions(address: str) -> str:
    host = urlsplit(address).hostname or ""
    links = f'<a href="{_escape(page_link(address))}">Try again</a>'
    if host and urlsplit(address).path not in ("", "/"):
        links += f'<a href="{_escape(page_link(f"gemini://{host}/"))}">Go to {_escape(host)}</a>'
    return f'<div class="actions">{links}</div>'


def _error_page(error: GeminiError, address: str) -> Response:
    return _notice(
        title="Could not load the page",
        address=address,
        heading=error.title,
        why=error.detail,
        actions=_retry_actions(address) if address.startswith("gemini://") else "",
        status="Not loaded",
    )


def _certificate_changed_page(error: CertificateChangedError, address: str) -> Response:
    form = (
        '<form method="post" action="/trust">'
        f'<input type="hidden" name="host_key" value="{_escape(error.host_key)}">'
        f'<input type="hidden" name="fingerprint" value="{_escape(error.new_fingerprint)}">'
        f'<input type="hidden" name="return_url" value="{_escape(address)}">'
        '<div class="actions"><button>Trust the new certificate and continue</button>'
        "<span>Or use the back button to leave.</span></div></form>"
    )
    fingerprints = (
        f'<p class="fingerprints">Saved: {_escape(error.previous_fingerprint[:32])}...<br>'
        f"Now: {_escape(error.new_fingerprint[:32])}...</p>"
    )
    main = f'<div class="notice"><h2>{_escape(error.title)}</h2><p class="why">{_escape(error.detail)}</p>{fingerprints}{form}</div>'
    return _page(
        title="Certificate changed",
        address=address,
        main=main,
        status="Not loaded: certificate changed",
    )


def _start_page() -> Response:
    visits = recent_visits(profile_dir(), RECENT_VISIT_COUNT)
    entries = (
        [(visit.title, visit.url) for visit in visits]
        if visits
        else list(STARTING_POINTS)
    )
    heading = "Recently visited" if visits else "Places to start"
    rows = "".join(
        f'<a class="link" href="{_escape(page_link(url))}"><span class="arrow">=&gt;</span>'
        f'<span class="label">{_escape(title)}</span><span class="host">{_escape(urlsplit(url).hostname or "")}</span></a>'
        for title, url in entries
    )
    main = (
        '<div class="empty"><h1>Gemini browser</h1><p class="hint">Type a gemini:// address above and press Enter.</p>'
        f'<div class="recent"><h3>{heading}</h3>{rows}</div></div>'
    )
    return _page(
        title="Gemini browser", address="", main=main, status="Nothing loaded yet"
    )


def _prompt_page(result: FetchResult) -> Response:
    sensitive = result.status == 11
    base = without_query(result.url)
    method = "post" if sensitive else "get"
    input_type = "password" if sensitive else "text"
    main = (
        '<div class="prompt"><strong>This page asks a question</strong>'
        f'<div style="color:#6d6753;margin-top:4px">{_escape(result.meta or "The site asked for input.")}</div>'
        f'<form action="/" method="{method}"><input type="hidden" name="url" value="{_escape(base)}">'
        f'<input name="input" type="{input_type}" autocomplete="off" autofocus aria-label="Your answer">'
        "<button>Send</button></form></div>"
    )
    host = urlsplit(result.url).hostname or ""
    status = f"{result.status} input requested &middot; {_escape(host)} &middot; {_escape(_certificate_note(result.certificate))}"
    return _page(title="Input requested", address=base, main=main, status=status)


def _parse_mime(meta: str) -> tuple[str, str]:
    pieces = [piece.strip() for piece in (meta or "text/gemini").split(";")]
    charset = "utf-8"
    for piece in pieces[1:]:
        name, _, value = piece.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip('"').lower()
    return pieces[0].lower() or "text/gemini", charset


def _decode(body: bytes, charset: str) -> str:
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _success_page(result: FetchResult, record: bool) -> Response:
    mime, charset = _parse_mime(result.meta)
    host = urlsplit(result.url).hostname or ""
    status = f"{result.status} {_escape(mime)} &middot; {_escape(host)} &middot; {_escape(_certificate_note(result.certificate))}"
    open_elsewhere = (
        f'<a href="{_escape(result.url)}">Open in another Gemini client</a>'
    )
    note = (
        '<p class="note">This page is longer than 8 MB; the rest is not shown.</p>'
        if result.truncated
        else ""
    )
    title = result.url
    source = ""
    if mime == "text/gemini" or mime.startswith("text/"):
        text = _decode(result.body, charset)
        if mime == "text/gemini":
            rendered = render_gemtext(text, result.url)
            main, title = rendered.html, rendered.title or result.url
        else:
            main, title = f'<pre class="plain">{html.escape(text)}</pre>', result.url
        main += note
        source = _source_panel(text)
    elif mime in IMAGE_TYPES:
        encoded = base64.b64encode(result.body).decode("ascii")
        main = f'<img class="picture" alt="{_escape(result.url)}" src="data:{mime};base64,{encoded}">{note}'
    else:
        size_kb = max(1, len(result.body) // 1024)
        download = f"/download?url={quote(result.url, safe='')}"
        main = (
            '<div class="notice"><h2>This is a file, not a page</h2>'
            f'<p class="why">{_escape(mime)}, about {size_kb} KB. This browser cannot show it.</p>'
            f'<div class="actions"><a href="{_escape(download)}">Download the file</a></div></div>'
        )
    if record:
        record_visit(profile_dir(), result.url, title, _now())
    return _page(
        title=f"{title} - Gemini browser",
        address=result.url,
        main=main,
        status=status,
        source=source,
        actions=open_elsewhere,
    )


def _failure_page(result: FetchResult) -> Response:
    heading, advice = (
        FAILURES.get(result.status)
        or FAILURES.get(result.status // 10 * 10)
        or ("The server could not show this page", "The server answered with an error.")
    )
    meta = result.meta.strip()
    why = advice
    if result.status == 44 and meta.isdigit():
        why = f"The server asks you to wait {meta} seconds before the next request."
    elif meta and result.status // 10 != 6:
        why = f"{advice} The server said: {meta}"
    host = urlsplit(result.url).hostname or ""
    return _notice(
        title=heading,
        address=result.url,
        heading=heading,
        why=why,
        actions=_retry_actions(result.url),
        status=f"{result.status} {meta} \u00b7 {host}",
    )


def _redirect_page(result: FetchResult) -> Response:
    target = resolve_link(result.url, result.meta)
    scheme = urlsplit(target).scheme.lower()
    if scheme in ("http", "https"):
        link = f'<a href="{_escape(target)}" target="_blank" rel="noopener noreferrer">Open {_escape(target)}</a>'
    else:
        link = f"<span>{_escape(target)}</span>"
    return _notice(
        title="Redirected away",
        address=result.url,
        heading="This page moved to another kind of address",
        why="It points somewhere a Gemini browser cannot open.",
        actions=f'<div class="actions">{link}</div>',
        status=f"{result.status} redirect",
    )


def _render_result(result: FetchResult, record: bool) -> Response:
    if result.status in (10, 11):
        return _prompt_page(result)
    if 20 <= result.status < 30:
        return _success_page(result, record)
    if result.status in REDIRECT_STATUSES:
        return _redirect_page(result)
    if 40 <= result.status < 70:
        return _failure_page(result)
    return _notice(
        title="Unexpected answer",
        address=result.url,
        heading="The server gave an answer this browser does not understand",
        why=f"It answered {result.status} {result.meta}.",
        actions=_retry_actions(result.url),
        status=f"{result.status}",
    )


def _load(
    address: str, user_input: str | None, record: bool, secret: bool = False
) -> Response:
    """Fetch the address (adding the person's answer to a prompt, if any) and show what came back.

    When the answer is a secret, a page that offers to try again or to trust a new certificate shows the address
    without it, so the secret never lands in a link, a form field or the app's own request log.
    """
    try:
        url = normalize_address(address)
    except GeminiError as error:
        return _error_page(error, "")
    if user_input is not None:
        url = replace_query(url, user_input)
    shown = without_query(url) if secret else url
    try:
        result = fetch(url, _verify_certificate, allow_private_addresses=may_open_private_addresses(), trusted_hosts=VISITOR_HOSTS)
    except CertificateChangedError as error:
        return _certificate_changed_page(error, shown)
    except GeminiError as error:
        return _error_page(error, shown)
    return _render_result(result, record)


@app.route("/", methods=["GET", "POST"])
def index() -> Response:
    """Open the address in the bar as a page, or show the start page when there is none.

    A POST carries the answer to a prompt that asks for a secret, so the answer never appears in an address.
    """
    if request.method == "POST":
        return _load(
            request.form.get("url", ""),
            request.form.get("input", ""),
            record=False,
            secret=True,
        )
    address = request.args.get("url", "").strip()
    if not address:
        return _start_page()
    return _load(address, request.args.get("input"), record=True)


@app.route("/download")
def download() -> Response:
    """Download a file from a Gemini server instead of showing it in the browser."""
    try:
        result = fetch(
            normalize_address(request.args.get("url", "")),
            _verify_certificate,
            allow_private_addresses=may_open_private_addresses(),
            trusted_hosts=VISITOR_HOSTS,
        )
    except GeminiError as error:
        return Response(
            f"{error.title}\n{error.detail}\n", status=502, mimetype="text/plain"
        )
    if not 20 <= result.status < 30:
        return Response(
            f"The server answered {result.status} {result.meta}\n",
            status=404,
            mimetype="text/plain",
        )
    name = (
        re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(urlsplit(result.url).path))
        or "download"
    )
    return Response(
        result.body,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.route("/trust", methods=["POST"])
def trust() -> WerkzeugResponse:
    """Save the certificate a site now presents, after the person chose to trust it, then reload the page."""
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        abort(403)
    host_key = request.form.get("host_key", "")
    fingerprint = request.form.get("fingerprint", "")
    return_url = request.form.get("return_url", "")
    if not HOST_KEY_PATTERN.fullmatch(host_key) or not FINGERPRINT_PATTERN.fullmatch(
        fingerprint
    ):
        abort(400)
    trust_host(profile_dir(), host_key, fingerprint, _now())
    return redirect(
        page_link(return_url) if return_url.startswith("gemini://") else "/", code=303
    )


@app.route("/app.js")
def app_script() -> Response:
    """The script behind the back and forward buttons."""
    return Response(APP_SCRIPT, mimetype="text/javascript")


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Where the browser reports what the page's security policy blocked."""
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in (
            "blocked-uri",
            "document-uri",
            "source-file",
            "effective-directive",
            "line-number",
            "script-sample",
        )
    )
    # A block caused by a browser extension is the policy doing its job and is not logged.
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(
        BROWSER_EXTENSION_SCHEMES
    ):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        "Content-Security-Policy blocked {!r} on {!r} ({}){}{}; {}",
        blocked,
        page,
        directive,
        where,
        starting,
        remedy,
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    setup_logging()
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
