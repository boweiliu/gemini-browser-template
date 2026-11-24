# Design notes

## Pieces

- `runner.py`: routes and page templates. `GET /` shows the start page or loads the address in `?url=`; a POST to `/`
  carries the answer to a secret prompt; `/download` saves a file; `/trust` replaces a saved certificate; `/app.js` is
  the back and forward script. Every page is server-rendered HTML.
- `gemini_client.py`: normalizes addresses, makes one TLS request, follows gemini redirects, resolves relative links.
- `gemtext.py`: gemtext lines to HTML, with every piece of page text escaped.
- `gemini_store.py`: the two JSON files, guarded by one lock and written by replacing the whole file.
- `gemini_errors.py`: errors that carry a title and a sentence of detail written for the person.

## Safety rules

1. Every piece of page text is escaped. Only `gemini`, `http` and `https` links are clickable; any other scheme
   (`javascript:`, `mailto:`) is shown as text.
2. A site's certificate is checked against the saved fingerprint right after the handshake, before the address is
   sent, so an impostor never learns what was asked for. A first visit saves the fingerprint. A different one is
   refused until the person trusts it.
3. A secret answer (status 11) travels by POST and is kept out of every error page and trust form, so it never lands
   in an address or the app's request log.
4. Who is asking comes from the identity header the workspace's proxies stamp. The owner (or a request with no
   account) browses anywhere and keeps the app's data directory; anyone else gets a directory of their own and may
   open only public addresses. Every address a name resolves to is checked, and the connection goes to one of those
   same addresses, so a name cannot be re-resolved to a private one between the check and the connect. Hosts the owner
   names in `GEMINI_BROWSER_VISITOR_HOSTS` are exempt.
5. `/trust` refuses a request a browser marks as coming from another site, and checks the host and fingerprint it is
   given.
6. The request guard from `app-schema` runs ahead of every route: a page of another origin is refused, and so is a
   header-less request that did not come from loopback.

## Data on disk

`known_hosts.json`: `{ "host:port": { fingerprint (SHA-256 hex), first_seen, last_seen } }`. `history.json`: a list of
`{ url, title, visited_at }`, newest first, at most 50. Both live per profile (see the README).

## What it does not do

Client certificates, per-person rate limits, an access log, caching of fetched pages. Pages are fetched on demand and
the original text is shown through "View source", so no page is stored.
