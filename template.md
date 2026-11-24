---
title: "Gemini browser"
description: "Browse any gemini:// address from a Studio window, with a profile of their own for each person it is shared with."
thumbnail: "template.svg"
version: v1
format: v2
---

# Gemini browser

This file is the manifest for the **Gemini browser** template (slug:
`gemini-browser`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

Browse any gemini:// address from a Studio window. Gemini is a small, text-first protocol (TLS on port 1965) with its
own page format, gemtext, that ordinary web browsers cannot open. This app is a Gemini browser for a Studio workspace:
type an address, read the page, follow links, answer input prompts, download files and see the original page text with
"View source". Pages come back as plain server-rendered HTML in a Studio window, with images shown inline. A site's
certificate is remembered the first time it is seen (trust on first use), and a changed certificate stops the page and
asks the person before trusting the new one. Each person the workspace is shared with has a profile of their own, with
their own history and their own saved certificates. The owner browses any address; visitors browse public addresses
only, plus any host the owner names in `GEMINI_BROWSER_VISITOR_HOSTS` (empty by default). Known gaps: no client
certificates (statuses 60 to 62 show an explanation) and no per-person rate limits.



## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/gemini_browser/`
- `system/supervisord.conf.d/gemini-browser.conf`

- `system/apps/gemini_browser/` is the app, a standalone uv project (its own lock and virtual environment). `app.toml`
  and `icon.svg` register it as a Studio window. `src/gemini_browser/runner.py` is a Flask app that renders every page
  server-side; `gemini_client.py` makes one TLS request per page and checks the certificate before the address is sent;
  `gemtext.py` turns gemtext into escaped HTML; `gemini_store.py` keeps each person's `known_hosts.json` and
  `history.json` under `data/.apps/gemini-browser/`. The tests sit beside the code and run against a local fake Gemini
  server with a self-signed certificate (`fake_gemini_server.py`), never the internet; the `browser` tests drive a real
  browser at desktop and phone sizes. `docs/` holds setup, operations, design and investigation notes, and
  `CHANGELOG.md` the app's own changes. The app depends on the workspace's own libraries `app-schema`
  (`system/libs/app_schema`, pointed at from its `pyproject.toml`) and `workspace-layout`.
- `system/supervisord.conf.d/gemini-browser.conf` is the supervisord program `gemini-browser`. It syncs the app's
  environment, registers the app's `app.toml` with `forward_port.py` on port 8081, then runs the `gemini-browser` entry
  point. Who is asking comes from the identity header the workspace's proxies stamp on each request.

The program runs the app in place under `system/apps/gemini_browser/`; the section "Install it as its own repo" below
moves it into a repository of its own.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


No activation requirements: the app needs no permissions, secrets or LLM access.

Adaptation:

- Outbound Gemini must work. The workspace has to make outbound TCP connections on port 1965; some networks block
  it. Check: `printf 'gemini://geminiprotocol.net/\r\n' | openssl s_client -connect geminiprotocol.net:1965 -servername geminiprotocol.net -quiet`
  prints a first line `20 text/gemini`. If it hangs or is refused, the app cannot work until that port is opened.
- Visitor hosts. Visitors may open public addresses only, because an address that resolves inside the workspace's
  network could otherwise be used to probe it. If you run a capsule of your own whose name resolves to a private
  address from inside the workspace, decide whether to name it for visitors: add
  `environment=GEMINI_BROWSER_VISITOR_HOSTS="your-host.example,..."` to `system/supervisord.conf.d/gemini-browser.conf`
  (see `docs/setup.md`, section 4). Left unset, visitors open public sites only.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `connect-external-service` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. Then follow "Install it as its own repo" below, so the installed copy ends up as a repository of its own.
7. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Install it as its own repo

The template keeps the app under `system/apps/gemini_browser/` (its published state). After you adopt it, give the
installed copy a repository and a history of its own, separate from the workspace's. Run these steps in order from the
workspace root.

1. **Move the app out of the workspace tree.** Copy `system/apps/gemini_browser/` (it carries its tests and docs) into
   a new directory `.external_worktrees/gemini-browser/`, with the package at the root: `app.toml`, `icon.svg`,
   `pyproject.toml`, `src/`, `docs/` and the test files beside them. Do not copy `.git` or `.venv`.
2. **Point it at the workspace's libraries and give it its own history.** In that copy, change the `app-schema` line
   under `[tool.uv.sources]` in `pyproject.toml` from `../../libs/app_schema` to `../../system/libs/app_schema`, then
   run `uv lock` there. Then `git init -b main` in `.external_worktrees/gemini-browser/` and make one first commit. Do
   not fetch, clone or merge anything from the template into it.
3. **Optional: a private GitHub repo for it.** Create your own private repository (`POST /user/repos` through
   latchkey, with no `auto_init`, `"private": true`) and push `main` through the gateway. Or keep it local only. Say
   which you chose.
4. **Wire config only.** Edit `system/supervisord.conf.d/gemini-browser.conf` so its command syncs and runs the app
   from the clone: `ensure-project` and `uv sync --locked --project .external_worktrees/gemini-browser`, then
   `forward_port.py --manifest .external_worktrees/gemini-browser/app.toml --url http://localhost:8081`, then
   `exec .external_worktrees/gemini-browser/.venv/bin/gemini-browser`. Then run
   `supervisorctl reread && supervisorctl update`.
5. **Remove the app's code from the workspace tree.** `git rm -r system/apps/gemini_browser` and commit. The
   supervisord file stays; it now points at the clone. `.external_worktrees/` is ignored by the workspace's own git, so
   the clone's history lives only in its own repository.
6. **Check that it separated.** `git -C .external_worktrees/gemini-browser log --oneline` lists only the app's own
   commit or commits; `ls system/apps/gemini_browser` fails; and `curl -s http://127.0.0.1:8081/health` answers
   `{"status":"ok"}`, served from the clone.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-11-24) -- first published version: the Gemini browser app and its supervisord program, with the install-as-its-own-repo steps

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
