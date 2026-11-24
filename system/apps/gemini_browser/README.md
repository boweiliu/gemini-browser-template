# gemini-browser

Browse any gemini:// address from a Studio window: type an address, read the page, follow links, answer input
prompts, and see the original page source. Built for a workspace owner who wants to read Gemini space, and for the
people the workspace is shared with, who each get a profile of their own.

## What it does

- Loads a gemini:// address and shows the page: headings, links, lists, quotes and preformatted blocks. Images show
  inline; other files get a download link.
- Follows redirects (up to 5), answers input prompts (status 10 by address, status 11 by POST so a secret never
  appears in an address), and shows plain-language pages for server errors and connection failures.
- Remembers each site's certificate the first time it is seen (trust on first use). A different certificate later
  blocks the page and asks the person whether to trust the new one.
- Keeps the pages the person opened and lists them on the start page. "View source" shows the original page text.
- The owner browses anywhere. Everyone else the workspace admits browses public sites only, plus any host the owner
  names in `GEMINI_BROWSER_VISITOR_HOSTS`, each with their own history and saved certificates.

## Architecture

One Flask app (`runner.py`) renders every page as server-side HTML: no framework, one small script for the back and
forward buttons. `gemini_client.py` makes one TLS request per page (self-signed certificates are expected, so trust is
decided by the saved fingerprint and the certificate is checked before the address is sent), `gemtext.py` turns
gemtext into escaped HTML, `gemini_store.py` keeps `known_hosts.json` and `history.json`, and `fake_gemini_server.py`
is the loopback Gemini server the tests run against.

## Where things live

| What | Where |
| --- | --- |
| The app | `src/gemini_browser/` |
| Its tests | `src/gemini_browser/*_test.py`, `conftest.py`, `fake_gemini_server.py`, `test_gemini_browser_ratchets.py` |
| App manifest and icon | `app.toml`, `icon.svg` |
| Supervisord program for a workspace | `system/supervisord.conf.d/gemini-browser.conf` (outside this directory) |
| Setup, operations, design, investigation | `docs/` |
| What changed | `CHANGELOG.md` |

## Run and test

Setup from nothing is in [docs/setup.md](docs/setup.md); day-two work is in [docs/operations.md](docs/operations.md).

Tests run from this directory: `uv run --no-sync pytest -m ''`. They never touch the internet: the app is pointed at a
fake Gemini server with a self-signed certificate on a loopback port and at a scratch data directory. The `browser`
tests drive the app in a real browser at desktop and phone sizes. Plain `pytest` skips them.

## What it keeps

Under `data/.apps/gemini-browser/` (override with `GEMINI_BROWSER_DATA_DIR`). The owner's files sit there directly:
`known_hosts.json` (one small entry per site, replaced only when the person trusts a new certificate) and
`history.json` (the last 50 pages, oldest dropped first). Each other person has the same two files under
`visitors/<account>-<hash>/`.
