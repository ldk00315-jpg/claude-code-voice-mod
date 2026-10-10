# claude-code-voice-mod

[日本語版 README](README.ja.md)

Talk to your Claude Code session by voice on Windows.

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
>   private keys, `password = ...` lines, credentials in connection strings, long random strings)
>   are replaced by `[REDACTED]` first, but this is **best effort**: it only knows common formats.
> - **Voice can misfire.** Background speech, a TV, or a misheard phrase can become a request to
>   Claude. Voice requests run **with the same permissions as your Claude Code session**.
> - **The guard is not a sandbox.** Dangerous actions in voice-originated turns are blocked by a
>   string-based guard (see below). It is easy to bypass (for example a script that deletes files)
>   and it only covers known patterns.
> - **Do not use it in folders that contain secrets**, credentials, or anything you would not want
>   read aloud or sent to OpenAI. Use a headset, and turn it off (`/voice off`) when you are not
>   using it.
>
> The software is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)).

## Demo flow

1. In Claude Code, type `/voice on docs-site`. An Edge app window ("[docs-site] Voice Mod") opens.
2. Press **Start (uses the microphone)** and talk: "Hi!" - the voice answers by itself.
3. Say "Check which tests are failing in this project and tell me why." The voice says a short
   filler ("Let me check that.") and hands the request off.
4. A turn starting with `[voice] ...` appears in your Claude Code session; the status line shows
   `voice: Claude working on "..."`. Claude works as usual (you can watch and approve prompts).
5. When the turn ends, the answer goes back to the voice, which speaks a one or two sentence summary.
   The full answer stays on screen.
6. `/voice off` when you are done.

## Architecture

```
 You (microphone / speaker)
   |  WebRTC audio (echo cancellation by the browser)
   v
 Edge page (helper/voice_page.html, served on 127.0.0.1)
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
- codex is started with `read-only` sandbox, `approval_policy="never"`, web search off, your
  configured MCP servers disabled and features such as apps, plugins, computer use and browser use
  disabled. MCP servers are turned off per name, twice: `-c mcp_servers.<name>.enabled=false` for
  the servers in `config.toml` (`%CODEX_HOME%` or `~/.codex`, read only, never modified), then a
  thread-level override for every server the effective config (`config/read`, all layers) still
  has enabled. codex merges `-c` tables, so one override cannot empty the whole list. If an MCP
  server still starts, the helper logs a WARN and prints an `mcp_warning` line. Any approval
  request from codex is declined.
- The local HTTP server accepts only `Host`/`Origin` of its own `127.0.0.1:<port>` and needs a
  random per-run token (`X-Voice-Token`) on every call except loading the page; the token is in
  the helper's `ready` line for the plugin and embedded in the page it serves. No CORS headers are
  sent, so other web pages cannot read it.
- More detail: [docs/how-it-works.md](docs/how-it-works.md).

## Requirements

- Windows 10 or 11 (the helper opens Edge and uses a Windows job object so codex never outlives it).
- Claude Code **2.1.289 or newer** (desktop app Code tab or CLI) with Mod support. Developed and
  tested with Claude Code 2.1.289 (live voice tests) and 2.1.293 (`claude plugin validate/test`).
- Microsoft Edge (the voice page runs as an Edge app window).
- [uv](https://docs.astral.sh/uv/) on `PATH` (it runs the helper; Python 3.11 to 3.13 is fetched
  automatically if needed). The helper uses the standard library only.
- The [codex CLI](https://github.com/openai/codex) on `PATH`, **logged in with ChatGPT**
  (`codex login`, check with `codex login status`). Tested with codex-cli 0.160.0.
  Verified only on a ChatGPT **Pro** plan; whether Free, Go, Plus or other plans can use the
  realtime voice path is unknown. Reports are welcome.
- A headset is strongly recommended (speaker playback can trigger the microphone).

## Install

Clone or download this repository to a folder of your choice, for example
`C:\tools\claude-code-voice-mod`. The repository root is the plugin; the helper lives in
`helper/` inside it, and the plugin finds it relative to its own folder (`$.plugin.root`), so
there is no path to configure.

**Option A: load it from the folder (recommended while trying it out)**

- CLI, one session: `claude --plugin-dir C:\tools\claude-code-voice-mod`
- Desktop app or every session: set the environment variable
  `CLAUDE_CODE_PLUGIN_DIRS=C:\tools\claude-code-voice-mod` (user environment, or the `env` block
  of `~/.claude/settings.json`), then restart Claude Code.

**Option B: install from a marketplace** (once the repository is on GitHub):

```
/plugin install voice --marketplace ldk00315-jpg/claude-code-voice-mod
```

Answer `y` to add the marketplace and pick a scope. An installed copy also works, because the
helper's data never lives in the plugin folder.

Check: `/voice status` should answer `voice: stopped`.

## Usage

| Command | What it does |
| --- | --- |
| `/voice on [name]` | Starts the helper and opens the voice page. `name` is the thread label (optional). |
| `/voice off` | Stops the helper (and codex). Also happens when the session ends. |
| `/voice status` | Phase, helper port, queued/running voice requests, whether the guard is active. |

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
thread is this?"). Give it with `/voice on <name>`, or rename it on the page. The label is
remembered per Claude Code session (in the plugin's `$.store`), across `/voice off`, restarts and
resumes. Without one, the label is `<folder>・<first 4 characters of the session id>`. A rename
reaches the voice model at the next **Start**.

## Configuration: persona and voice

The helper keeps its data **outside the plugin folder**, in a per-user folder:

- `%LOCALAPPDATA%\claude-code-voice-mod\` by default, or the folder in the environment variable
  `VOICE_MOD_DATA_DIR`.
- `settings.json` (voice and persona), `logs\voice-*.log`, `logs\voice-answers.jsonl` (metadata
  only: id, status, length and a short hash, no answer text) and an empty `codex-cwd\` folder
  used as codex's working directory.
- The log never holds answer bodies (only id, length and a short hash). Transcripts and requests
  are kept for debugging, with secret-looking strings redacted (best effort).

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

All fields are optional. The persona is read when the helper starts (`/voice off` then `/voice on`
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
  happened to the request meanwhile (timed out, `/voice off`). A typed prompt that contains the
  whole marker (for example a pasted voice prompt) is guarded too: fail-safe by design.
- The guard's state lives in the module: if the plugin hot-reloads during a voice turn, tracking of
  that turn is lost (new voice turns are tracked normally).
- It is a seat belt, not a sandbox. Do not rely on it for security.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `/voice on` says the helper exited at once | `uv` and `codex` on `PATH`? Run `uv run --script <plugin folder>\helper\voice_helper.py --help`. |
| `codex not found on PATH` | Install the codex CLI, or check `where codex`. |
| `realtime state=error` / 401 / `unauthorized` | `codex login status` must show a ChatGPT login. Log in again. |
| `realtime conversation requires API key auth` | The WebRTC transport was not used: your codex version may have changed the API. |
| `AVAS realtime calls require realtime v1 or v3` | Only v1/v3 work over ChatGPT login (the default is v3). |
| Page shows "failed" on Start | Microphone permission in Edge, default recording device, another app holding the mic. Open the page only via `127.0.0.1`. |
| `media-failed` / `media lost` | A firewall or VPN may block UDP. |
| No sound | Edge tab muted? Output device? |
| The voice interrupts itself | Use a headset (echo). |
| The voice answers by itself instead of delegating | Ask more concretely ("look at the files in..."); adjust `persona.instructions`. |
| Answer not spoken | The status line says why (`answer not spoken: ...`); the page log shows `Result -> speech`. |
| Anything else | `%LOCALAPPDATA%\claude-code-voice-mod\logs\voice-*.log` (SDP credentials and secret-looking strings are redacted and answers are not logged, but transcripts and requests are in it: review before sharing). |

## Development

```
claude plugin validate .
claude plugin test .
uv run --project helper python -m unittest discover -s helper/tests
```

Headless self tests that talk to the real service (they use your ChatGPT plan; no microphone):
`uv run --project helper --group selftest helper/voice_helper.py --selftest-text "Hello"` and
`--selftest-handoff "..."` (see the helper's docstring).

## License

MIT, see [LICENSE](LICENSE).
