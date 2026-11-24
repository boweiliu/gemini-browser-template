# Setup, from nothing

The browser needs no account, key or outside service. It needs a Studio workspace that can open outbound TCP
connections on port 1965 (the Gemini port) and a place to keep two small files.

Each step ends with a check that proves it worked.

## 1. Put the app in the workspace

Inside a workspace the app is a normal Studio app: a folder with `app.toml`, `icon.svg`, `pyproject.toml` and
`src/gemini_browser/`, run by a supervisord program that registers it with the shell.

- Standalone project: `uv lock` and `uv sync --locked` inside the app folder give it its own virtual environment.
- Program: `system/supervisord.conf.d/gemini-browser.conf`. It runs `forward_port.py --manifest <app>/app.toml --url
  http://localhost:8081` and then the `gemini-browser` entry point. Then `supervisorctl reread && supervisorctl update`.
- The app depends on the workspace's own `app-schema` library (route table, request guard) and on `workspace-layout`
  (who is asking). In the app folder, `[tool.uv.sources]` points `app-schema` at the workspace's
  `system/libs/app_schema`; adjust that path to where the app sits relative to the workspace.

Check: `curl -s http://127.0.0.1:8081/health` answers `{"status":"ok"}`, and `workspace-layout list` lists the app.

## 2. Check outbound Gemini works

From inside the workspace:

```
printf 'gemini://geminiprotocol.net/\r\n' | openssl s_client -connect geminiprotocol.net:1965 \
  -servername geminiprotocol.net -quiet
```

Check: the first line of the answer is `20 text/gemini`. If the connection hangs or is refused, the network the workspace
runs on blocks port 1965 outbound; the browser cannot work until that is opened.

## 3. Open it

`workspace-layout open gemini-browser`, then type an address. Check: `geminiprotocol.net` loads a page titled
"Project Gemini", and the start page then lists it under "Recently visited".

## 4. Optional: let visitors open one of your own hosts

Visitors may open public sites only, because an address that resolves inside the workspace's network could otherwise be
used to probe it. If you run a capsule whose name resolves to a private address from inside the workspace (common when
the workspace and the capsule share a host), name it for visitors in the program's environment:

```
environment=GEMINI_BROWSER_VISITOR_HOSTS="capsule.example.org,other.example.org"
```

Then `supervisorctl reread && supervisorctl update`. Check: with a visitor identity header (see operations), that host
loads and an address like `gemini://127.0.0.1/` still answers "This address is not open to visitors".

## 5. Optional: serving your own capsule

Not part of this app. If you run a Gemini server of your own on the same machine, port 1965 must also be open in the
hosting provider's firewall for people outside to reach it. Verify from outside the machine, not from inside: a machine
often cannot reach its own public address.
