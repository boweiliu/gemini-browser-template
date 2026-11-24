# Operations

## Restart and refresh

After a code change, `uv run --no-sync system/scripts/workspace_scripts/verify_app.py gemini-browser --restart
--marker "Gemini browser" --refresh` restarts the program, waits for the port, checks the page and refreshes open
windows. After a change to the program file (port, environment), `supervisorctl reread && supervisorctl update`
restarts it with the new definition.

## Where things are

- Logs: `/var/log/supervisor/gemini-browser-stdout.log` and `-stderr.log`. Page blocks by the security policy show up
  in the error log as `Content-Security-Policy blocked ...`.
- Data: `data/.apps/gemini-browser/` (`known_hosts.json`, `history.json`; visitors under `visitors/`). Small files, no
  other store. Back up with the rest of the workspace; nothing is rebuildable from the internet.
- Settings: `GEMINI_BROWSER_DATA_DIR`, `GEMINI_BROWSER_PORT`, `GEMINI_BROWSER_VISITOR_HOSTS` (environment of the
  program).

## What the pages mean

| The person sees | Cause | What to do |
| --- | --- | --- |
| Can't find that server | The name does not resolve | Check the spelling |
| The server refused the connection | Reachable but nothing on port 1965 | The capsule is down; start it |
| The server did not answer | Timeout, usually a firewall dropping the port | Check the capsule's firewall from outside the machine |
| The secure connection failed | The server does not speak TLS on that port | Not a Gemini server |
| This site's certificate has changed | The site shows a different certificate than the saved one | Normal after a renewal; use the trust button only if the change is expected |
| This address is not open to visitors | A visitor opened an address inside the workspace's private network | Intended; add the host to `GEMINI_BROWSER_VISITOR_HOSTS` if it is yours |
| This page needs a client certificate | Status 60 to 62 | Client certificates are not supported |
| The request's identity could not be used | The identity header named an account id that is not filename-safe | A proxy bug; nothing is saved |

## Limits

One request per page; 30 seconds in total and 15 seconds of silence; at most 5 redirects; a page is cut off at 8 MiB
with a note; an address may be at most 1024 bytes. There is no rate limit per person (the owner chose none).

## Visitor and owner profiles

The identity header the workspace's proxies stamp decides the profile: the owner (or a request with no account) uses
`data/.apps/gemini-browser/` directly; everyone else uses `visitors/<account>-<hash>/`. To look at a visitor's pages,
read that folder's `history.json`. Deleting a visitor's folder resets their history and saved certificates.

To act as a visitor in a test: send the header
`X-Imbue-Identity: {"owner": false, "user_id": "someone", "email": "someone@example.org"}` to the app on loopback.
That writes a profile folder under the live data, so do it against a scratch `GEMINI_BROWSER_DATA_DIR`.
