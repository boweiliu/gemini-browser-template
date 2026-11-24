# Changelog

## 2026-11-24

- First version: browse any gemini:// address; gemtext pages, links, input prompts, redirects, error pages, images,
  file downloads, view source, recent pages.
- Certificates remembered on first use, with a page that asks before trusting a changed one.
- Hardened with tests against a local fake Gemini server (not the internet), including phone-size browser checks.
- Opened to everyone the workspace admits: each person has their own history and saved certificates; visitors open
  public sites only.
- `GEMINI_BROWSER_VISITOR_HOSTS` lets the owner name hosts visitors may open even when they resolve privately.
