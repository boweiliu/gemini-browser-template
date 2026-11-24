# Investigation notes

What was found while building this, so nobody repeats it. Dates are 2026-11-24.

## A capsule that looked down

A Gemini client outside the workspace could not open the owner's capsule (a Gemini server on a hosted machine,
port 1965 published by the host).

- Tried: connecting from inside the workspace. It worked, but only because the workspace's resolver answers the
  capsule's name with the host's private gateway address. That says nothing about outside reach.
- Tried: connecting from inside the workspace to the host's public address. Every port, including 80 and 443,
  answered "connection refused", so the machine cannot reach its own public address. Not a valid test either.
- Worked: an outside port checker (check-host.net, four nodes in different countries). Port 443 connected, port 1965
  timed out. A timeout, not a refusal, means a firewall dropping the port. The cause was the hosting provider's
  firewall, which only allowed the ports the host's setup lists (80, 443, 53). After the owner opened inbound TCP 1965,
  all four nodes connected.
- Lesson: verify a public service from outside the machine that runs it.

## Outbound Gemini from the workspace

Works on port 1965 to public capsules (geminiprotocol.net, gemini.circumlunar.space, kennedy.gemi.dev all answered
`20 text/gemini`). Gemini needs raw TLS, so the browser cannot go through the workspace's HTTP proxies and connects out
itself.

## Visitors and private addresses

Opening the browser to people the workspace is shared with lets them make the workspace connect to any host and port.
Left alone, that is a way to probe the workspace's private network. Visitors are limited to public addresses, checked
per resolved address. Side effect: the owner's own capsule resolves to a private address from inside the workspace, so
it needs naming in `GEMINI_BROWSER_VISITOR_HOSTS` to be open to visitors.

## Deliberately not done

- No rate limit per person: the owner chose none.
- No client certificate support: status 60 to 62 show an explanation instead.
- Trust on first use only, as Gemini clients do; no certificate authority checks.
