# Changelog

## 0.3.0 (2026-10-10)

Fixes for the findings of a security re-review of 0.2.1 (commit 553e449).

- **Guard survives a hot reload (F1).** The voice-origin guard (running turn, voice and typed
  subagents, submitted voice texts) is saved in the session's `$.state` (`voice.guard`, contract in
  `types/index.d.ts`) on every change and restored by a reloaded module before its first decision.
  If it cannot be read, the running turn is guarded until the next turn starts or ends. The
  `tool.call` guard denies the blocked kinds of calls when it fails before deciding.
- **MCP isolation fails closed (F4).** MCP servers are disabled with one TOML inline table at the
  CLI stage and one nested object at `thread/start`, so names with dots (`corp.files`) are covered.
  `config/read`, the MCP status list and "every server disabled" are required before the helper
  reports ready; any MCP startup later stops the realtime session and refuses new ones
  (`mcp_blocked`, shown in the status line, a toast and the page). README wording now says what is
  enforced.
- **Redaction and logs (F5).** Secret-named values (`password`, `pin`, `token`, `api_key`, ...) are
  hidden in every form (JSON, env, `:`/`=`, quoted, prose such as "the password is 123456"), short
  numbers included; only an explicit list of harmless names (`max_tokens`, `timeout`, `port`, ...)
  keeps numbers. Conversation logging is off by default (metadata only); `--debug-log` or
  `"debugLog": true` turns it on. Logs older than 7 days, and the oldest past 20 MB, are deleted at
  startup.
- **Page link and answers (F2).** The page opens only through a one-time link (`/?k=...`), which it
  removes from the address bar; `/voice page` opens a new one. `POST /answer` accepts only requests
  the helper sent in the current realtime session. Same-user processes are documented as out of
  scope.
- **HTTP limits (F6).** Content-Length checks and per-path body limits, JSON object and field type
  checks, a 10 s read timeout, at most 8 concurrent handlers (503), and realtime start/stop
  serialised with a session generation.
- Plugin version 0.3.0; README notes that a typed prompt containing the full voice marker is
  guarded too.

## 0.2.1 (2026-10-10, commit 553e449)

Hardening after the first security review.

- Voice turns are recognised by the voice marker and by every submitted text, so a request that
  timed out or was dropped by `/voice off` is still guarded; subagents of voice turns stay guarded.
- Local HTTP: a per-run token on every call but the page, strict `Host`/`Origin`, no CORS.
- MCP isolation: `CODEX_HOME` respected, effective config read with `config/read`, thread-level
  overrides, startup status reported.
- Answers are redacted before they go to the realtime model; answer bodies are not logged.

(The plugin manifest still said 0.2.0 for this commit.)

## 0.2.0 (2026-10-08, commit 2dbfc29)

Initial release: voice front desk for Claude Code on Windows (codex app-server realtime with a
ChatGPT login, Edge page, request queue, voice-origin guard, persona settings).
