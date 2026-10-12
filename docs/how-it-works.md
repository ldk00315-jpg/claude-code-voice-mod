# How it works

Notes on the design, distilled from the investigation behind this project. Everything here was
checked against **codex-cli 0.160.0** (the generated `codex app-server generate-ts --experimental`
types and the `openai/codex` sources at tag `rust-v0.160.0`) and Claude Code 2.1.289 to 2.1.295.
The codex realtime API is experimental: any of this can change.

## Pieces

| Path | Role |
| --- | --- |
| `.claude-plugin/plugin.json` | The plugin `voice`. |
| `hooks/register.ts` | `/voice on/off/page/status`, spawning the helper, the request queue, turn correlation, posting answers back, wiring the guard (saved in `$.state`), the thread label. |
| `types/index.d.ts` | The `$.state` contract: `voice.guard`, the guard's scope kept across a hot reload. |
| `hooks/guard.ts` | The voice-origin guard: pure functions plus `VoiceScope`. |
| `hooks/prompt.ts` | The `[voice] ...` prompt submitted to Claude Code. |
| `tests/*.test.ts` | `claude plugin test`: guard rules, a full round trip against a fake helper, and hot reloads in the middle of a voice turn. |
| `helper/voice_helper.py` | Runs `codex app-server`, relays SDP, turns handoffs into requests and answers into speech. Standard library only. |
| `helper/voice_page.html` | The browser page (Edge app window on Windows): microphone, speaker, voice picker, preview, thread label, log. |
| `helper/tests/` | Offline unit tests for the helper (no codex, no network). |

## Audio path: the browser is the WebRTC endpoint

`thread/realtime/start` accepts `transport: {type: "webrtc", sdp: <offer>}` and returns the answer
SDP in a `thread/realtime/sdp` notification. So a client can bring its own WebRTC offer, and the
helper simply relays the page's offer and codex's answer over `127.0.0.1`. Audio then flows between
the browser and OpenAI directly; neither codex nor the helper touches it.

Using the browser gives echo cancellation, a jitter buffer and packet loss concealment for free.
A Python WebRTC peer (aiortc) has no echo cancellation, so on speakers the model would interrupt
itself; it is only used for the headless self tests.

Over ChatGPT login (codex's "AVAS" path) only realtime **v1** and **v3** work; the helper uses v3 by
default (`--version v1` switches). v3 has no text output modality, and its data channel events are
named `session.*`, `turn.*`, `output_transcript.*` (not v1's `response.*`), so the page does not
depend on them except for waiting on `session.started` before a preview.

## codex does no work

- Started as `codex app-server -c sandbox_mode="read-only" -c approval_policy="never"
  -c web_search="disabled" -c model_reasoning_effort="low" -c notify=[]` plus
  one `-c mcp_servers={"<name>"={enabled=false}, ...}` naming every MCP server in `config.toml`
  under `CODEX_HOME` (default `~/.codex`; read, never written; an entry for a name that is not
  configured would have no transport, which codex rejects), and
  `--disable hooks multi_agent apps plugins computer_use browser_use in_app_browser
  image_generation memories js_repl goals`.
- `thread/start`: read-only sandbox, `approvalPolicy: "never"`, `ephemeral: true`, an empty working
  folder, and developer instructions that say "do nothing, reply OK".
- MCP servers (verified against codex rust-v0.160.0 and a live `codex app-server` 0.160.0):
  - codex merges `-c` tables into the user's config (`config/src/merge.rs`), so `-c mcp_servers={}`
    cannot replace the table, and there is no switch for all servers at once.
  - A `-c` key path is split at every dot, with no quoted keys (`config/src/overrides.rs`
    `apply_toml_override`), so `mcp_servers."corp.files".enabled` would address the wrong table.
    The value is parsed as TOML (`utils/cli/src/config_override.rs`), where a quoted key may hold
    dots, so the helper passes **one** inline table: `-c mcp_servers={"corp.files"={enabled=false},
    clock={enabled=false}}`. (A second `mcp_servers=` would replace the first inside the CLI layer.)
  - `thread/start` `config` keys are applied the same way, chained after the process's `-c`
    overrides into one CLI layer (`app-server/src/config_manager.rs` `load_with_cli_overrides`).
    The helper passes one nested object, `{"mcp_servers": {"<name>": {"enabled": false}, ...}}`,
    naming **every** server of the effective config (`config/read`: system, user, profile, project
    layers), because it replaces the CLI layer's own `mcp_servers` table.
  - App and plugin servers are off through `--disable apps plugins`.
  - Fail closed: `config/read` must succeed, and after `thread/start` the paged
    `mcpServerStatus/list` for the thread must succeed with every server `disabled`; otherwise the
    helper prints `{"type":"mcp_blocked","reason":...}` and exits without a `ready` line. Any
    `mcpServer/startupStatus/updated` notification later (a server starting anyway) prints
    `mcp_blocked`, stops the realtime session and refuses new ones; the plugin shows it in the status
    line and a toast, the page in a red notice.
- `thread/realtime/start`: `includeStartupContext: false` (no workspace context for the voice model),
  `clientManagedHandoffs: true` (codex does not feed its own output to the voice model), and the
  helper's own `prompt` (it replaces codex's default backend prompt, so it must say when to delegate).
- A delegation always starts a codex turn; the helper interrupts it immediately (about 0.1 s in
  tests). Approval requests are declined, MCP elicitations are declined, and any other server request
  is refused.
- On Windows, codex is started from its native `codex.exe` (not the npm shim) and put in a Windows job object
  with `KILL_ON_JOB_CLOSE`, so it dies with the helper even if the helper is killed. The helper exits
  on `POST /shutdown`, Ctrl+C/Ctrl+Break, a closed stdout (parent gone, detected by a 5 s heartbeat),
  codex exiting, or after `--idle` seconds without HTTP traffic.
- **macOS (experimental, not tested on a real Mac).** The same helper runs on POSIX with these
  differences: codex is started with `start_new_session=True`, so codex and its children form one
  process group; every exit of the helper goes through `AppServer.close`, which closes codex's stdin
  and then sends `SIGTERM` to the group and `SIGKILL` after 2 s if anything is left. A watchdog
  process (`voice_helper.py --watch-group <pgid>`, started with `python -I` in its own session)
  blocks on a pipe from the helper; when that pipe closes (the helper exited or was killed hard) or
  its parent changes, it kills the group the same way. The helper also stops when it is re-parented
  (its parent process is gone) and on `SIGINT`, `SIGTERM` or `SIGHUP`. The page is opened with
  `open -na "<App>.app" --args --app=<url>` for the first of Google Chrome, Microsoft Edge, Brave
  Browser or Chromium found in `/Applications` or `~/Applications`, else `open <url>` (the default
  browser); argv is a list (no shell), the browser gets no access to the helper's stdout, and the
  URL is never logged. Data lives in `~/Library/Application Support/claude-code-voice-mod`. Linux
  shares these code paths but is not supported.

## Handoff -> Claude Code -> speech

1. The voice model emits a delegation. codex reports it as `thread/realtime/itemAdded` with an item of
   type `handoff_request`. With v3, `input_transcript` carries the model's own wording of the task;
   the helper also attaches the last few final user utterances, and prints one stdout line:
   `{"type":"request","id":...,"text":...,"userSaid":[...]}`.
2. The plugin queues it and submits `[voice] <text>` with `$.prompt.submit`, one request at a time
   (a session runs one turn at a time anyway).
3. A plugin's own hooks do not see its own `$.prompt.submit`, so the voice turn is recognised at
   `turn.start` by its text containing the submitted prompt; its `turnId` is recorded. Only the
   main-loop `turn.complete` of that turn (no `agentId`) counts as the answer. Whether the turn is
   guarded does not depend on the queue: the voice marker (`[voice] ` at a line start plus the
   notice block, `hooks/prompt.ts`) or any text the plugin submitted (the last 50, kept in the
   session's `$.state`) is enough. So a voice prompt still queued in Claude Code after its request timed out or
   after `/voice off` runs guarded; its answer is posted as `late` if the helper still runs, else
   dropped. A typed prompt containing the whole marker is guarded too (fail-safe).
4. The plugin posts `{id, answer, status, reason, turnId, durationMs}` to the helper's
   `POST /answer`. `status` is `ok`, `aborted`, `error`, `refusal`, `timeout` (10 minutes) or `late`.
5. codex 0.160.0 has **no client call that completes a specific handoff with a result**: with
   `clientManagedHandoffs: true`, codex's own handoff completion returns early, and the realtime
   client calls are only `start`, `appendAudio`, `appendText`, `appendSpeech`, `stop` and
   `listVoices`. In v3 both `appendText` and `appendSpeech` become a `session.context.append` on the
   sideband socket (`appendSpeech` with channel `speakable`). So the helper appends the trimmed
   answer (code blocks removed, cut near 2000 characters) as text starting with `[RESULT]` (or
   `[FAILED]`), and the voice prompt says what to do with such messages. If no assistant transcript
   follows within 12 s, the same text is sent once more with `appendSpeech`.

Findings from testing: text appended with `appendText` is treated as context and did **not**
trigger delegation; a spoken request (or a TTS wav fed as the microphone in the self test) did.

## Voice-origin guard scope

`VoiceScope` marks the main loop as voice-originated from that turn's `turn.start` to its main-loop
`turn.complete`. Every subagent spawned in it (`agent.spawn` with a voice parent, or an unknown
`agentId` first seen while the voice turn runs) stays voice-originated for its whole life, including
background agents that keep running after the voice turn ended. `checkVoiceToolCall` then decides by
tool name and, for anything with a string `command`, by regular expressions per command segment.

A hot reload of the plugin runs `register` of a fresh module: its variables start over (and the
helper child is killed), but the session's `$.state` stays. The scope (main turn id and whether it
is voice, the voice and typed subagent ids) and the submitted texts are therefore saved as
`voice.guard` (contract in `types/index.d.ts`) on every change, before the hook lets the event go on,
and a new load restores them before its first guard decision. If the saved value cannot be read,
the running turn and every subagent first seen during it are treated as voice-originated until the
next main-loop `turn.start` or `turn.complete`. The saved snapshot is not trusted alone: unless it
already guards the main loop, the new load reads `$.session.messages()` and, if the prompts of the
turn still running (after the last final answer or interrupt note) carry the full voice marker,
guards that turn the same way. An empty transcript, or one ending in a final answer, guards
nothing, so a fresh typed session is never blocked; if the transcript cannot be read, the snapshot
stands. A save advances the saved revision only once `$.state.set` succeeded; a failed save keeps
the scope dirty, is retried at every hook entry (`turn.start`, `tool.call`, `agent.spawn`,
`turn.complete`, `/voice status`) and shows `guard NOT saved (retrying)` in the status line and one
toast until a save lands. If the `tool.call` guard itself fails before deciding, its catch handler
denies the blocked kinds of calls whatever the turn.

Voice-origin subagent ids are never evicted; only typed ones are capped (500, newest kept). An id
is a few dozen bytes, so the snapshot grows by about 40 bytes per voice subagent (about 400 KB for
10,000 in one session) plus the submitted texts (at most 50 texts and 200,000 characters).
`tests/reload.test.ts` loads the module twice over one state store to check this.

## Thread label

`/voice on <name>` passes `--label` to the helper; the page can rename it (`POST /label`), and the
helper reports the new name on stdout so the plugin stores it in `$.store` under the session id.
The label goes into the page title and heading and into the voice prompt.

## Data and privacy

- Sent to OpenAI: microphone audio, transcripts, request texts, and Claude's trimmed answers.
- Before an answer goes to the realtime model, secret-looking strings are replaced by `[REDACTED]`
  (`redact_secrets` in the helper): API keys (`sk-...`, `ghp_...`, `github_pat_...`, `xox?-...`,
  `AKIA...`, ...), JWTs, `Bearer`/`Basic` credentials, PEM private keys, `password|secret|token|
  api_key ... = value` assignments, credentials in URLs and connection strings, and long hex or
  random base64 strings (32+ characters). This is best effort: unusual formats get through, and
  harmless long ids such as commit hashes may be hidden.
- A secret-named assignment hides any value, short numbers included: `password = 123456`,
  `"pin": 1234`, `API_KEY=12345`, `pwd: '42'`, "the password is 123456", `パスワードは1234`. A name
  is secret when a part of it (split at `_ - .` and camelCase) is `pwd`, `pin` or `otp`, or it
  contains `password`, `passwd`, `passphrase`, `passcode`, `secret`, `token`, `apikey`,
  `accesskey`, `privatekey`, `credential` or `authorization`. Only an explicit list of harmless
  names (`max_tokens`, `prompt_tokens`, `token_count`, `timeout`, `port`, ...) keeps a numeric value.
- Local logs (in the data folder) hold **metadata only** by default: states, errors, ids, server
  names, and the length of every other text (`metadata()` in the helper). `--debug-log` or
  `"debugLog": true` in `settings.json` adds transcripts and request texts, passed through the same
  redaction plus SDP ICE credentials, fingerprints and candidate IPs. Answer bodies are never
  logged: the log line for an `appendText`/`appendSpeech` holds only its length and a short SHA-256
  prefix, and `voice-answers.jsonl` records only id, status, length, that hash and turn id. At
  startup, log files older than 7 days are deleted, then the oldest until all fit in 20 MB.
- The helper's HTTP server listens on `127.0.0.1` only. Every request must carry `Host`
  `127.0.0.1:<port>` or `localhost:<port>` (no DNS rebinding); an `Origin`, when present, must be
  exactly `http://127.0.0.1:<port>` or `http://localhost:<port>`. `GET /` needs `?k=<key>`, a
  one-time key (`secrets.token_urlsafe(32)`, valid once and for 5 minutes, never logged) that the
  helper opens the browser with; `/voice page` (`POST /reopen`) mints a new one. The page then drops it
  from the address bar (`history.replaceState`). Every other path except `/favicon.ico` needs
  `X-Voice-Token`, a random per-run token (compared in constant time). The plugin gets it from the
  `ready` line; the page gets it embedded in a `<meta>` tag. No CORS headers are sent; responses
  carry `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. Refusals are `403`.
- `POST /answer` takes only ids of requests the helper printed in the current realtime session
  (unknown: `403`, an earlier session: `409`); nothing is recorded for a refused id.
- Limits: bodies must be JSON objects within a per-path size (16 KB, 128 KB for `/answer`, 256 KB
  for `/offer`; `413` past it), with typed fields (`400`); the socket read timeout is 10 s; at most
  8 requests are handled at once (`503`); realtime start and stop run one at a time under an
  asyncio lock, and every start or stop bumps a session generation.
- Out of scope: other processes of the same OS user. They can read the token and the key from
  the plugin's or the browser's memory or command line.

## Known limits

- Delegation is the model's judgement: it can answer something itself that needed work, or delegate
  small talk. Tune with the persona's `instructions`.
- Consecutive requests are told apart only by the request text quoted in `[RESULT] ...`.
- Each delegation starts (and immediately interrupts) a codex turn, which may count slightly against
  your ChatGPT plan usage.
- Claude's permission prompts must be answered on screen.
- Requests run one at a time; parallel work would need one subagent per request and a new guard design.
