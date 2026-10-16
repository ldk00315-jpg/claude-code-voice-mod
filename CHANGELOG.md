# Changelog

## 0.4.1 (2026-10-16)

- **The command is now `/talk`** (`/talk on [name]`, `/talk off`, `/talk status`, `/talk page`).
  Claude Code has a built-in `/voice` ("Toggle voice mode", available only on claude.ai). From
  Claude Code 2.1.299 the built-in takes precedence, so `/voice on` answered
  "/voice isn't available in this environment" and never reached this plugin. The plugin name
  (`voice`) and the install commands are unchanged.

## 0.4.0 (2026-10-12)

Experimental macOS support and an install guide that Claude Code can follow. The Windows behaviour
is unchanged. The new macOS code paths were added after the security-reviewed `v0.3.1` and have
**not been security-reviewed or tested on a real Mac** (unit tests with mocks only, run on Windows).

- **macOS (experimental).** The helper opens the voice page in an app window of Google Chrome,
  Microsoft Edge, Brave or Chromium when one is installed (`open -na <App> --args --app=<url>`),
  else in the default browser (`open <url>`); argv only, no shell, the URL (one-time key) never
  logged, the browser detached and kept off the helper's stdout. codex runs in its own process
  group (`start_new_session`); the helper sends `SIGTERM` and then `SIGKILL` to the group on every
  exit, and a watchdog process (`--watch-group`) kills the group if the helper dies without
  cleaning up. The helper stops on `SIGINT`/`SIGTERM`/`SIGHUP` and when its parent process is
  gone. Data folder: `~/Library/Application Support/claude-code-voice-mod` (other POSIX systems:
  `${XDG_STATE_HOME:-~/.local/state}/claude-code-voice-mod`; `VOICE_MOD_DATA_DIR` and `--data-dir`
  still win). Linux shares these code paths but is not supported.
- Unit tests for the platform branches (`helper/tests/test_platforms.py`): data folders, browser
  commands for each case including the fallback, process-group kill, watchdog, signals; the Windows
  launch command, spawn flags and signals are pinned to their previous values.
- **README (en/ja): "Install with Claude Code".** A prompt to paste into Claude Code, numbered install
  steps an agent can follow (prerequisite checks with exact commands per OS, install, reload,
  first run) and rules for the agent: never handle credentials (`codex login` is the user's),
  ask before any change to settings or environment variables, never widen auto-approval.
- **Install commands verified** against the Claude Code docs and `claude plugin install --help`
  (2.1.295): `/plugin install voice --marketplace ldk00315-jpg/claude-code-voice-mod` (in a session,
  2.1.275+) was already right; the README now also gives the shell forms
  `claude plugin marketplace add ldk00315-jpg/claude-code-voice-mod` +
  `claude plugin install voice@claude-code-voice-mod`, and
  `claude plugin install voice --marketplace ...` (2.1.292+).
- README: requirements per OS, macOS notes (browser, microphone permission, data folder, Homebrew),
  macOS rows in Troubleshooting; the "Security reviews" section says what 0.4.0 adds outside the
  reviews.
- ChatGPT plans, tested 2026-10-12 with codex-cli 0.160.0: Pro works, **Free does not** (the
  realtime endpoint returns `404 Not Found`), Go/Plus untested. New README section "Check whether
  your ChatGPT plan works" (self test, how to read its output, testing another account with a
  separate `CODEX_HOME`, the "State mismatch" fix).
- Self-test command fixed: `uv run --project helper --group selftest python helper/voice_helper.py`
  (without `python`, uv uses the script's inline metadata, ignores `--group selftest` and the test
  fails with `No module named 'aiortc'`).
- Texts that named Edge only (the `/voice on` reply, the used-up link page, helper help) are
  platform-neutral now. Version 0.4.0 in the plugin, the marketplace, the helper project and
  `clientInfo`.

## Documentation after review 4 (unreleased on its own, included in 0.4.0)

Documentation after the fourth security review of 0.3.1 (commit deeac60); no behaviour change.

- README (en/ja): "Experimental release for small trials" in the disclaimer; the two known gaps of
  the guard while its state cannot be saved (transcript also unreadable; a voice turn's background
  subagent still running after the turn ended) and what to do while `guard NOT saved` is shown;
  a "Security reviews" section with the four rounds and the reviewer's verdict.
- Code comments in `hooks/register.ts` describe the same gaps where the guard is restored.
- The helper reports version 0.3.1 to `codex app-server` (`clientInfo`), matching the plugin.

## 0.3.1 (2026-10-11)

Fixes for the remaining findings of a third security review of 0.3.0 (commit d3a47fa).

- **Failed guard saves (R1).** A save advances the saved revision only after `$.state.set`
  succeeded; a failed save stays dirty, is retried at every hook entry, and is shown in the status
  line (`guard NOT saved (retrying)`) and one toast. The running module keeps guarding the turn in
  memory. After a hot reload the saved snapshot is cross-checked with the transcript
  (`$.session.messages()`): a running main turn whose prompt carries the voice marker is guarded
  even when the snapshot is missing or an older typed one. A fresh typed session is not affected.
- **Old answers in a new realtime session (R2).** The delayed `appendSpeech` re-send carries the
  realtime session generation, re-checks it after every wait and, under the start/stop lock, right
  before sending; a stop or a new start cancels pending re-sends. The first `appendText` of an
  answer is checked and sent under the same lock.
- **Long sessions (R3).** Voice-origin subagent ids are never evicted (only typed ones are capped
  at 500); an unknown agent seen during a voice turn counts as voice.
- Docs: the page says logs are pruned when the helper starts; the README calls the guard an
  auxiliary safety net, not a security boundary, and says to keep Claude Code's permission prompts
  and not to use the Mod with data that must not leave the machine. Helper version aligned (0.3.1).

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
