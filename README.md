# claude-code-voice-mod

[日本語版 README](README.ja.md)

Talk to your Claude Code session by voice on Windows (macOS: experimental, not tested on a real Mac).
Version 0.4.0 ([CHANGELOG](CHANGELOG.md)).

This is a Claude Code **Mod** (a plugin made of function hooks) plus a small Python helper.
A realtime voice model from OpenAI, reached through `codex app-server` with your ChatGPT
login, acts as a voice front desk: it chats with you, and when you ask for real work
("look at the logs and tell me why the build fails") it hands the request to your open
Claude Code session. When Claude finishes, the voice reads you a one or two sentence summary.

> [!WARNING]
> ## Disclaimer and risks: read this first
>
> - **Experimental, undocumented API.** This project drives the *experimental* realtime API of
>   `codex app-server` (`thread/realtime/*`, enabled with `experimentalApi: true`) using
>   **ChatGPT-plan authentication**. It is not a public, documented or supported interface. It was
>   tested with **codex-cli 0.160.0** and may break with any codex update, without notice.
> - **Not affiliated.** This project is not affiliated with, endorsed by, or supported by OpenAI
>   or Anthropic. "Claude", "Claude Code", "OpenAI", "ChatGPT" and "Codex" are trademarks of their
>   owners. codex is only invoked as an external program; no OpenAI or Anthropic code is included.
> - **Your responsibility.** You are responsible for making sure your use complies with OpenAI's
>   and Anthropic's terms of service and usage policies (including how a ChatGPT plan may be used).
>   If you are unsure, do not use it.
> - **Data sent to OpenAI.** Your microphone audio, its transcripts, the requests handed to Claude,
>   and **Claude's answers (trimmed to about 2000 characters, code blocks removed)** are sent to
>   OpenAI so the voice can summarise them. Anything in those answers, including file contents or
>   paths Claude mentions, leaves your machine. Secret-looking strings (API keys, tokens, JWTs,
>   private keys, values of secret-named settings such as `password = 1234`, `"pin": 0000` or
>   "the password is ...", credentials in connection strings, long random strings) are replaced by
>   `[REDACTED]` first, but this is **best effort**: it only knows common formats and cannot
>   recognise every secret or piece of personal information. Your microphone audio goes from the
>   page to OpenAI directly and is never redacted.
> - **Voice can misfire.** Background speech, a TV, or a misheard phrase can become a request to
>   Claude. Voice requests run **with the same permissions as your Claude Code session**.
> - **The guard is an auxiliary safety net, not a security boundary.** Dangerous actions in
>   voice-originated turns are blocked by a string-based guard (see below). It is easy to bypass
>   (for example a script that deletes files) and it only covers known patterns. Keep Claude Code's
>   normal permission prompts on; do not widen auto-approval because the guard exists.
> - **Experimental release for small trials.** Use it yourself or share it with a few people who
>   understand the points above. It has had four rounds of security review (see
>   [Security reviews](#security-reviews)); some edge cases of the guard remain open, and nothing
>   was tested end to end against the live services by the reviewers. Do not present it as
>   something that reliably prevents dangerous actions.
> - **Do not use it with data that must not leave your machine**, nor in folders that contain
>   secrets, credentials, or anything you would not want read aloud or sent to OpenAI (turning
>   logging off does not change what is sent). Use a headset, and turn it off (`/talk off`) when
>   you are not using it.
>
> The software is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)).

## Demo flow

1. In Claude Code, type `/talk on docs-site`. A browser app window ("[docs-site] Voice Mod") opens
   (Edge on Windows; on macOS Chrome, Edge, Brave or Chromium, else your default browser).
2. Press **Start (uses the microphone)** and talk: "Hi!" - the voice answers by itself.
3. Say "Check which tests are failing in this project and tell me why." The voice says a short
   filler ("Let me check that.") and hands the request off.
4. A turn starting with `[voice] ...` appears in your Claude Code session; the status line shows
   `voice: Claude working on "..."`. Claude works as usual (you can watch and approve prompts).
5. When the turn ends, the answer goes back to the voice, which speaks a one or two sentence summary.
   The full answer stays on screen.
6. `/talk off` when you are done.

## Architecture

```
 You (microphone / speaker)
   |  WebRTC audio (echo cancellation by the browser)
   v
 browser page (helper/voice_page.html, served on 127.0.0.1)
   |  SDP offer/answer over local HTTP             audio flows page <-> OpenAI directly
   v
 helper/voice_helper.py  --stdio JSON-RPC-->  codex app-server  --(ChatGPT login)-->  OpenAI realtime
   |  stdout JSON lines ("request", ...)            (sideband control of the same call)
   |  <- POST /answer {id, answer, status}
   v
 voice plugin (hooks/register.ts) inside Claude Code
   |  queue (one at a time) -> $.prompt.submit("[voice] ...")
   |  turn.start / turn.complete correlation -> answer posted back
   |  voice-origin guard on tool.call (hooks/guard.ts)
   v
 your Claude Code session does the work
```

- The voice model decides between answering itself (small talk) and delegating (anything that
  needs the computer). A delegation arrives as a `handoff_request`; codex is told to do nothing and
  its turn is interrupted at once, so **Claude Code is the only worker**.
- codex is started with `read-only` sandbox, `approval_policy="never"`, web search off, and
  features such as apps, plugins, computer use and browser use disabled. Any approval request from
  codex is declined.
- **MCP isolation (what is enforced).** Every MCP server codex knows of is disabled by name, at two
  stages: one `-c mcp_servers={"<name>"={enabled=false}, ...}` override for the servers in
  `config.toml` (`%CODEX_HOME%` or `~/.codex`, read only, never modified), then a thread/start
  override naming every server in the effective config (`config/read`, all layers). Names with dots
  or other special characters (`corp.files`, `service@home`) are covered. The helper then
  **requires** that `config/read` and the MCP status list (`mcpServerStatus/list`) both succeed and
  that every listed server is `disabled`; otherwise it refuses to start (an `mcp_blocked` line, a
  toast and the status line say why). If any MCP server reports a startup later, the realtime
  session is stopped and new ones are refused, and the page shows a red "Voice is blocked" notice.
  This checks what codex reports; it is not a sandbox around codex.
- The local HTTP server accepts only `Host`/`Origin` of its own `127.0.0.1:<port>`. The page is
  served only through a **one-time link** (`/?k=...`) that the helper opens the browser with; the page
  removes it from the address bar, and `/talk page` opens a fresh one. Every other call needs a
  random per-run token (`X-Voice-Token`) that the plugin gets from the helper's `ready` line and
  the page carries. No CORS headers are sent. Answers are accepted only for requests the helper
  itself sent in the current realtime session. Bodies are size-limited and type-checked, reads time
  out, and at most 8 requests are handled at once.
- **Out of scope:** other programs running as the same OS user. They can read the token and
  the link from process memory or command lines, so this design does not protect against them.
- More detail: [docs/how-it-works.md](docs/how-it-works.md).

## Requirements

- **Operating system:** Windows 10/11 (tested) · macOS (**experimental**: implemented but **not
  tested on a real Mac**; reports welcome). Other systems are not supported.
- Claude Code **2.1.289 or newer** (desktop app Code tab or CLI) with Mod support. Developed and
  tested with Claude Code 2.1.289 (live voice tests) and 2.1.293; 0.3.0 to 0.4.0 were checked with
  2.1.295 (`claude plugin validate/test`).
- A browser for the voice page:
  - Windows: Microsoft Edge (the page runs as an Edge app window).
  - macOS: Google Chrome, Microsoft Edge, Brave or Chromium (in `/Applications` or
    `~/Applications`) gives an app-style window; without one the page opens in your default
    browser (Safari supports the WebRTC audio, but has no app window).
- [uv](https://docs.astral.sh/uv/) on `PATH` (it runs the helper; Python 3.11 to 3.13 is fetched
  automatically if needed). The helper uses the standard library only.
- The [codex CLI](https://github.com/openai/codex) on `PATH`, **logged in with ChatGPT**
  (`codex login`, check with `codex login status`). Tested with codex-cli 0.160.0.
  **ChatGPT plans (tested 2026-10-12, codex-cli 0.160.0):**

  | Plan | Result |
  | --- | --- |
  | Pro | Works |
  | Free | **Does not work**: the realtime endpoint answers `404 Not Found` |
  | Go, Plus, others | Not tested yet; may or may not work. Check with the [plan check](#check-whether-your-chatgpt-plan-works) and please report. |
- A headset is strongly recommended (speaker playback can trigger the microphone).

**macOS notes (experimental).**

- Install `uv` and `codex` with Homebrew (`brew install uv`, `brew install --cask codex`) or their
  official installers (uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`; codex:
  `npm install -g @openai/codex`). They must be on the `PATH` that Claude Code itself sees: if you
  start the desktop app from the Dock and `/talk on` says the helper exited at once, start Claude
  Code from a Terminal (`claude`) instead.
- Microphone permission: the first time you press **Start**, the browser asks for the microphone;
  macOS may also ask. If it was denied, allow it in **System Settings → Privacy & Security →
  Microphone** for that browser (Chrome, Edge, Brave, Chromium or Safari) and restart the browser.
- Data folder: `~/Library/Application Support/claude-code-voice-mod/` (see Configuration).
- codex runs in its own process group; the helper terminates the whole group when it exits, and a
  small watchdog process kills it if the helper itself is killed. This has only been unit-tested
  with mocks on Windows.

## Install

### Install with Claude Code (easiest)

Open Claude Code (any folder) and paste this, for example:

> Read the README.md of https://github.com/ldk00315-jpg/claude-code-voice-mod and install it on my
> computer. Ask me before changing any settings or environment variables.

Claude Code then follows the steps below. You still log in to codex yourself, approve the commands
it asks to run, and start a new session (or run `/reload-plugins`) at the end.

### Install steps (for people and for Claude Code)

> [!IMPORTANT]
> **Rules for an AI agent following these steps**
>
> - **Never handle credentials.** Do not run `codex login` for the user, and do not read, copy,
>   print or edit `~/.codex/auth.json` or any token, API key or password. If `codex login status`
>   does not show a ChatGPT login, stop and ask the user to run `codex login` themselves.
> - **Ask before every change outside this repository**: installing software, adding the
>   marketplace, installing the plugin, setting environment variables (`CLAUDE_CODE_PLUGIN_DIRS`,
>   `VOICE_MOD_DATA_DIR`), or editing `~/.claude/settings.json` or any other settings file. Show the
>   exact command first and wait for a yes.
> - **Do not widen auto-approval.** Do not add `permissions.allow` rules, switch to a bypass or
>   auto-accept permission mode, or use `--dangerously-skip-permissions`. The voice guard is only an
>   extra net on top of Claude Code's normal permission prompts.
> - Do not run `/talk on` or open the microphone for the user; tell them how (step 5).
> - At the end, report what you checked and what you changed.

**1. Check the operating system.** Windows 10/11, or macOS (experimental: tell the user it has not
been tested on a real Mac). On anything else, stop.

**2. Check the prerequisites.** The commands are the same in Windows PowerShell and the macOS
Terminal.

| Check | Command | Expected | If it fails |
| --- | --- | --- | --- |
| Claude Code | `claude --version` | `2.1.289` or newer (`2.1.292` or newer for the one-command shell install in step 3) | Update Claude Code. If `claude` is not found (desktop app only), use the in-session commands of step 3. |
| uv | `uv --version` | any version | Windows: `winget install --id=astral-sh.uv -e`. macOS: `brew install uv`. Or the [official installer](https://docs.astral.sh/uv/getting-started/installation/). Ask first. |
| codex | `codex --version` | `codex-cli ...` (tested with 0.160.0) | `npm install -g @openai/codex`, or on macOS `brew install --cask codex`. Ask first. |
| codex login | `codex login status` | logged in **using ChatGPT** (not an API key) | The **user** runs `codex login` and signs in with ChatGPT. The agent never does this. |
| Browser | - | Windows: Edge (built in). macOS: Chrome, Edge, Brave or Chromium recommended; the default browser otherwise | Optional: install one of them. |

After installing `uv` or `codex`, open a new terminal so `PATH` is updated, and check again.

**3. Install the plugin (recommended: from the GitHub marketplace).** This installs at user scope
(every project on this machine) and records it in `~/.claude/settings.json`, so ask first.

From a shell (an agent can run these after the user agrees):

```
claude plugin marketplace add ldk00315-jpg/claude-code-voice-mod
claude plugin install voice@claude-code-voice-mod
```

or, with Claude Code 2.1.292 or newer, in one command:

```
claude plugin install voice --marketplace ldk00315-jpg/claude-code-voice-mod
```

Or inside a Claude Code session (typed by the user; Claude Code 2.1.275 or newer):

```
/plugin install voice --marketplace ldk00315-jpg/claude-code-voice-mod
```

Answer `y` to add the marketplace, then pick a scope. The two-step form also works in a session:
`/plugin marketplace add ldk00315-jpg/claude-code-voice-mod`, then
`/plugin install voice@claude-code-voice-mod`. (`voice` is the plugin's name, `claude-code-voice-mod`
the marketplace's name in `.claude-plugin/marketplace.json`. Syntax: Claude Code docs,
[Install and manage plugins](https://code.claude.com/docs/en/discover-plugins).)

Alternative, without a marketplace (for development or trying a local copy): clone or download the
repository to a folder, for example `C:\tools\claude-code-voice-mod` or
`~/tools/claude-code-voice-mod`, then either

- start one CLI session with `claude --plugin-dir <folder>`, or
- (ask first) set the environment variable `CLAUDE_CODE_PLUGIN_DIRS=<folder>` (user environment, or
  the `env` block of `~/.claude/settings.json`; several folders are separated by `;` on Windows and
  `:` on macOS), then restart Claude Code.

The helper lives in `helper/` inside the plugin and is found relative to the plugin folder
(`$.plugin.root`), so there is no path to configure. Its data never lives in the plugin folder, so
an installed copy works as well.

**4. Load it.** Plugins installed from a shell load in the next Claude Code session; in a session
that is already open, run `/reload-plugins`. An agent cannot restart its own session: tell the user
to start a new session (desktop app: a new session; after changing an environment variable, restart
the app). Check with `claude plugin list` (shows `voice@claude-code-voice-mod`) and, in a session,
`/talk status`, which should answer `voice: stopped`.

**5. First run (the user does this).** In a Claude Code session, type `/talk on`. The voice page
opens; press **Start (uses the microphone)** and allow the microphone (macOS: also System Settings →
Privacy & Security → Microphone for that browser). Say "Hello" - the voice should answer. Type
`/talk off` when done. If something fails, see [Troubleshooting](#troubleshooting).

## Usage

> The command is `/talk` (until 0.4.0 it was `/voice`). Claude Code's own built-in `/voice` takes
> precedence from Claude Code 2.1.299 and answers "/voice isn't available in this environment".

| Command | What it does |
| --- | --- |
| `/talk on [name]` | Starts the helper and opens the voice page. `name` is the thread label (optional). |
| `/talk off` | Stops the helper (and codex). Also happens when the session ends. |
| `/talk page` | Opens the voice page again (a fresh one-time link), for example after closing the window. |
| `/talk status` | Phase, helper port, queued/running voice requests, whether the guard is active. |

On the voice page:

- **Start (uses the microphone)** begins a conversation; **Stop** ends it; **Mute** mutes the mic.
- **Type to talk** sends text into the conversation. Note: typed text is added as context and, in
  testing, did **not** trigger delegation; ask for work by voice.
- Requests are handled **one at a time**. Later ones queue. A request that takes more than 10
  minutes is answered "timeout" (the voice tells you); if Claude finishes later, the late answer is
  still spoken.
- Claude's permission prompts cannot be answered by voice. Approve them on screen.

### Voice picker and preview

The **Voice** list comes from codex (`thread/realtime/listVoices`; a built-in list is used if that
fails). The choice is saved to `settings.json` (see Configuration) and used next time. The voice is
fixed for a running conversation, so the list is locked while one runs. **Preview** asks the model
for a one-sentence self-introduction in the selected voice. It does not use the microphone (it sends
a silent track) and stops by itself.

### Thread label

The label names the Claude Code thread the window talks to, so several voice windows can be told
apart. It is shown in the window title and heading, and the voice model is told about it ("which
thread is this?"). Give it with `/talk on <name>`, or rename it on the page. The label is
remembered per Claude Code session (in the plugin's `$.store`), across `/talk off`, restarts and
resumes. Without one, the label is `<folder>・<first 4 characters of the session id>`. A rename
reaches the voice model at the next **Start**.

## Configuration: persona and voice

The helper keeps its data **outside the plugin folder**, in a per-user folder:

- `%LOCALAPPDATA%\claude-code-voice-mod\` by default on Windows,
  `~/Library/Application Support/claude-code-voice-mod/` on macOS, or the folder in the environment
  variable `VOICE_MOD_DATA_DIR` (or the helper's `--data-dir`).
- `settings.json` (voice and persona), `logs\voice-*.log`, `logs\voice-answers.jsonl` (metadata
  only: id, status, length and a short hash, no answer text) and an empty `codex-cwd\` folder
  used as codex's working directory.
- **Conversation logging is off by default.** The log keeps metadata only: states, errors, ids,
  server names and the length of each text, never what was said, requested or answered. For
  debugging, start the helper with `--debug-log` or put `"debugLog": true` in `settings.json`: then
  transcripts and requests are logged too, with secret-looking strings redacted (best effort).
  Answer bodies are never logged.
- Retention, applied each time the helper starts (not while it is stopped): log files older than 7 days are deleted, then
  the oldest ones until all of them fit in 20 MB; `voice-answers.jsonl` is rotated past 1 MB.
  To delete everything, remove the `logs` folder while the helper is stopped.

By default the assistant is a **neutral, friendly voice assistant that answers in the language you
speak** (for example Japanese or English). To give it a character, add a `persona` section to
`settings.json` (copy [helper/settings.example.json](helper/settings.example.json)):

```json
{
  "voice": "maple",
  "persona": {
    "name": "Mika",
    "userName": "Alex",
    "language": "Japanese",
    "style": "bright, casual and warm, like a friendly classmate; short sentences, a little playful",
    "fillers": ["見てくるから少し待っててね", "OK, let me look into that"],
    "instructions": "Call the user's computer 'the workshop'. Never use more than two sentences.",
    "previewText": "この声で、ひとことだけ自己紹介して。"
  }
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | none | What the assistant calls itself. |
| `userName` | none ("the user") | How it addresses you. |
| `language` | the language you speak | Force one language, for example `"Japanese"`. |
| `style` | friendly, calm, short | Speaking style, free text (a dialect or character voice goes here). |
| `fillers` | "Let me check that." | Short phrases said right after handing work to Claude (up to 6). |
| `instructions` | none | Extra free text appended to the voice model's instructions. |
| `previewText` | a one-sentence self-introduction | What the Preview button asks the model to say. |

All fields are optional. The persona is read when the helper starts (`/talk off` then `/talk on`
to apply a change). The persona only shapes the voice model; Claude Code itself receives a neutral
`[voice] ...` prompt and answers in the language of the request.

## Voice-origin guard

While a voice-originated turn runs, and in every subagent it starts (including background agents
that outlive it), `tool.call` **denies**:

| Kind | Examples |
| --- | --- |
| Recursive / destructive deletes | `rm -r`/`-rf`/`--recursive`, `del /s`, `rmdir /s`, `rd /s`, `Remove-Item -Recurse` (and short forms), `format X:`, `Format-Volume`, `Clear-Disk`, `diskpart`, `mkfs`, `git clean -f`, `git reset --hard` |
| Push | `git push` (also `git -C x push`) |
| GitHub CLI writes | `gh pr create/merge/close/edit/comment/review/...`, `gh issue create/close/...`, `gh release`, `gh repo edit/delete/create/...`, gist/secret/variable/workflow writes, `gh api` with a write method or fields |
| Package publishing | `npm/pnpm/yarn/bun publish`, `uv/poetry/flit/hatch/pdm publish`, `twine upload`, `cargo publish`, `gem push`, `nuget push`, `vsce publish`, `docker push` |
| Sending | `Send-MailMessage`, `sendmail`, `mail -s`, `curl` with POST/PUT/PATCH/DELETE or data/form/upload flags, `Invoke-WebRequest/RestMethod -Method Post...`, `wget --post-*` |
| Publishing | `Artifact` publish/delete/pin (anything but read/list/open/quickstart), `ArtifactData` writes, `ArtifactComments` posts |
| Browser / screen control | `mcp__computer-use__*`, `mcp__Claude_Browser__*`, `mcp__claude-in-chrome__*`, `mcp__Control_Chrome__*` |
| Scheduled tasks | `schtasks /create /change /delete /run`, `*-ScheduledTask`, `crontab`, `CronCreate/Delete/Update`, `RemoteTrigger`, `ScheduleWakeup`, scheduled-task MCP tools (except list/get), the `schedule`/`loop` skills |
| Outward MCP tools | MCP tools whose name contains `send`, `post`, `publish`, `delete`, `trash`, `share` or `create_event`; `set_setting`, `set_session_permission_mode` |

A blocked call shows a toast (`voice: blocked ...`) and tells Claude to mention it in its answer, so
the voice can tell you to ask again from the keyboard. **Typed turns are never checked.**

Limits:

- It is string matching on the command line, checked per `|`, `;`, `&&` segment. It does not look
  inside scripts (`python cleanup.py` that deletes files is not stopped), and it errs on the safe
  side for text that merely contains a dangerous phrase (`echo "rm -rf"` is blocked).
- Reading and editing files is allowed. A misheard request can still change files.
- A turn counts as voice-originated when its prompt carries the voice marker (a line starting
  with `[voice] ` plus the plugin's notice block), or is a text the plugin submitted, whatever
  happened to the request meanwhile (timed out, `/talk off`).
- **A typed prompt that contains the full voice marker** (for example a pasted voice prompt) is
  guarded too: fail-safe by design. Retype the request without the marker to run it unguarded.
- The guard's state (the running turn, which subagents belong to voice turns, the submitted
  texts) is saved in the session's `$.state` on every change, so a hot reload of the plugin in the
  middle of a voice turn keeps the guard. If that state cannot be read after a reload, the running
  turn and the subagents first seen during it are treated as voice turns until the next turn
  starts or ends (fail-safe). If saving fails, the status line shows `guard NOT saved (retrying)`
  (and a toast once); the guard keeps working in the running module and the save is retried at
  every later hook. A reloaded module also reads the transcript: if the running turn's prompt
  carries the voice marker, that turn is guarded even when the saved state is missing or older.
- **Known gaps while saving fails.** If the state could not be saved *and* the plugin is reloaded,
  the guard can be lost in two cases: (a) the transcript cannot be read either, so the running
  voice turn is not recognised; (b) a background subagent started by a voice turn keeps running
  after that turn has ended (the transcript no longer shows a running voice turn, and the
  subagent's origin was never saved). So **while `guard NOT saved` is shown, do not reload or
  update the plugin and do not leave background work running**: wait until the warning clears, or
  stop with `/talk off` and let running work finish under Claude's normal permission prompts.
- Subagents of voice turns are remembered for the whole session (never evicted), as long as the
  state can be saved (see above). Subagents of typed turns are remembered up to 500; one forgotten
  and seen again during a voice turn counts as voice.
- It is an auxiliary safety net, not a security boundary. Do not rely on it for security.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `/talk on` says the helper exited at once | `uv` and `codex` on `PATH`? Run `uv run --script <plugin folder>\helper\voice_helper.py --help`. macOS: start Claude Code from a Terminal so it sees the Homebrew `PATH`. |
| `codex not found on PATH` | Install the codex CLI, or check `where codex` (macOS: `which codex`). |
| `realtime state=error` / 401 / `unauthorized` | `codex login status` must show a ChatGPT login. Log in again. |
| `realtime conversation requires API key auth` | The WebRTC transport was not used: your codex version may have changed the API. |
| `AVAS realtime calls require realtime v1 or v3` | Only v1/v3 work over ChatGPT login (the default is v3). |
| macOS: no window opens | Install Chrome, Edge, Brave or Chromium, or check that your default browser opens `http` links. Then `/talk page`. |
| macOS: the microphone is silent or refused | System Settings → Privacy & Security → Microphone: allow your browser, restart it, then `/talk page`. |
| Page shows "failed" on Start | Microphone permission in the browser (Edge on Windows), default recording device, another app holding the mic. Open the page only via `127.0.0.1`. |
| `media-failed` / `media lost` | A firewall or VPN may block UDP. |
| No sound | Browser tab muted? Output device? |
| The voice interrupts itself | Use a headset (echo). |
| The voice answers by itself instead of delegating | Ask more concretely ("look at the files in..."); adjust `persona.instructions`. |
| Answer not spoken | The status line says why (`answer not spoken: ...`); the page log shows `Result -> speech`. |
| `voice: blocked: ...` / "Voice is blocked" on the page | MCP isolation could not be confirmed (see Architecture). Check the reason in the log; remove or fix the MCP server named there, then `/talk off` and `/talk on`. |
| The page says "this link is used up" | The page opens only once per link. Run `/talk page`. |
| Anything else | `%LOCALAPPDATA%\claude-code-voice-mod\logs\voice-*.log` (macOS: `~/Library/Application Support/claude-code-voice-mod/logs/`) (metadata only by default; with debug logging, transcripts and requests are in it: review before sharing). |

## Development

```
claude plugin validate .
claude plugin test .
uv run --project helper python -m unittest discover -s helper/tests
```

Headless self tests that talk to the real service (they use your ChatGPT plan; no microphone):
`uv run --project helper --group selftest python helper/voice_helper.py --selftest-text "Hello"` and
`--selftest-handoff "..."` (see the helper's docstring). Note the `python` before the script path:
the helper has inline script metadata, so `uv run ... helper/voice_helper.py` ignores
`--group selftest` and the test fails with `No module named 'aiortc'`.

### Check whether your ChatGPT plan works

Before installing, or if the voice never connects, run the self test once (no microphone; it sends
the text "Hello" to OpenAI and uses a little of your plan). In the repository folder:

```
uv run --project helper --group selftest python helper/voice_helper.py --selftest-text "Hello"
```

The first run downloads the test-only packages (aiortc etc.) and can take a minute or two.

| Output | Meaning |
| --- | --- |
| a line with `"type": "transcript", "role": "assistant"` and `"assistantTranscript": true` | Your plan works. |
| `unexpected status 404 Not Found ... realtime/calls` | Your plan cannot use the realtime voice (this is what a Free plan returns). |
| `401` / `unauthorized` | Not logged in: run `codex login` (yourself) and try again. |

To test **another ChatGPT account** without touching your normal login, use a separate codex home
in one PowerShell window (all steps in that same window):

1. `$env:CODEX_HOME = "$env:TEMP\codex-plan-test"; New-Item -ItemType Directory -Force $env:CODEX_HOME | Out-Null; codex login`
2. Close the browser tab that opens by itself (it uses your normal account). Copy the
   `https://auth.openai.com/...` URL printed in the window, open a private/incognito window, paste
   it there and sign in with the account to test.
3. `codex login status` should print `Logged in using ChatGPT`.
4. Run the self test above.
5. Close the window and delete `%TEMP%\codex-plan-test` (it holds that account's login).

If you get "State mismatch" in the browser, an older `codex login` is still waiting: stop it
(Ctrl+C in its window) and start again from step 1.

## Security reviews

The project went through four rounds of third-party security review (static review plus mock-based
probes; no live API calls, microphone recordings or penetration test). Each round's findings were
fixed in the next release; see [CHANGELOG.md](CHANGELOG.md).

| Round | Reviewed commit | Outcome |
| --- | --- | --- |
| 1 | `2dbfc29` (0.2.0) | Guard bypass after timeout/`/talk off`, unauthenticated local HTTP, MCP leak, unredacted answers. Fixed in 0.2.1. |
| 2 | `553e449` (0.2.1) | Mid-turn hot reload, token readable from the page, dotted MCP names, logging on by default, HTTP limits. Fixed in 0.3.0. |
| 3 | `d3a47fa` (0.3.0) | Unsaved guard state lost on reload, stale answer re-sent into a new session, eviction after 500 agents. Fixed in 0.3.1. |
| 4 | `deeac60` (0.3.1, tag `v0.3.1`) | Earlier fixes confirmed. Remaining: the two "known gaps while saving fails" listed under [Voice-origin guard](#voice-origin-guard). No real secrets, no unauthenticated remote code execution found. |

**0.4.0 is not covered by these reviews in full.** The macOS code paths (browser launch, process
group and watchdog, data folder, POSIX signals) and the README install steps were added after the
reviewed `v0.3.1` and have **not been security-reviewed or tested on a real Mac**; they are covered
only by unit tests with mocks, run on Windows. The Windows code paths are unchanged in behaviour.

Reviewer's verdict for 0.3.1: fine for your own use and for small trials by people who understand
the data sent to OpenAI, the shared Claude permissions and the experimental API; **not** fine to
distribute as a guard that reliably prevents dangerous actions. Real Claude Code hot reloads,
live Codex/OpenAI sessions and other OS users' access were not tested by the reviewers.

## License

MIT, see [LICENSE](LICENSE).
