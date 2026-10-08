# How it works

Notes on the design, distilled from the investigation behind this project. Everything here was
checked against **codex-cli 0.160.0** (the generated `codex app-server generate-ts --experimental`
types and the `openai/codex` sources at tag `rust-v0.160.0`) and Claude Code 2.1.289 to 2.1.293.
The codex realtime API is experimental: any of this can change.

## Pieces

| Path | Role |
| --- | --- |
| `.claude-plugin/plugin.json` | The plugin `voice`. |
| `hooks/register.ts` | `/voice on/off/status`, spawning the helper, the request queue, turn correlation, posting answers back, wiring the guard, the thread label. |
| `hooks/guard.ts` | The voice-origin guard: pure functions plus `VoiceScope`. |
| `hooks/prompt.ts` | The `[voice] ...` prompt submitted to Claude Code. |
| `tests/*.test.ts` | `claude plugin test`: guard rules, and a full round trip against a fake helper. |
| `helper/voice_helper.py` | Runs `codex app-server`, relays SDP, turns handoffs into requests and answers into speech. Standard library only. |
| `helper/voice_page.html` | The Edge page: microphone, speaker, voice picker, preview, thread label, log. |
| `helper/tests/` | Offline unit tests for the helper (no codex, no network). |

## Audio path: the browser is the WebRTC endpoint

`thread/realtime/start` accepts `transport: {type: "webrtc", sdp: <offer>}` and returns the answer
SDP in a `thread/realtime/sdp` notification. So a client can bring its own WebRTC offer, and the
helper simply relays the page's offer and codex's answer over `127.0.0.1`. Audio then flows between
Edge and OpenAI directly; neither codex nor the helper touches it.

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
  `-c mcp_servers.<name>.enabled=false` for every MCP server in `~/.codex/config.toml` (read, never
  written; an override for a name that is not configured would be rejected by codex), and
  `--disable hooks multi_agent apps plugins computer_use browser_use in_app_browser
  image_generation memories js_repl goals`.
- `thread/start`: read-only sandbox, `approvalPolicy: "never"`, `ephemeral: true`, an empty working
  folder, and developer instructions that say "do nothing, reply OK".
- `thread/realtime/start`: `includeStartupContext: false` (no workspace context for the voice model),
  `clientManagedHandoffs: true` (codex does not feed its own output to the voice model), and the
  helper's own `prompt` (it replaces codex's default backend prompt, so it must say when to delegate).
- A delegation always starts a codex turn; the helper interrupts it immediately (about 0.1 s in
  tests). Approval requests are declined, MCP elicitations are declined, and any other server request
  is refused.
- codex is started from its native `codex.exe` (not the npm shim) and put in a Windows job object
  with `KILL_ON_JOB_CLOSE`, so it dies with the helper even if the helper is killed. The helper exits
  on `POST /shutdown`, Ctrl+C/Ctrl+Break, a closed stdout (parent gone, detected by a 5 s heartbeat),
  codex exiting, or after `--idle` seconds without HTTP traffic.

## Handoff -> Claude Code -> speech

1. The voice model emits a delegation. codex reports it as `thread/realtime/itemAdded` with an item of
   type `handoff_request`. With v3, `input_transcript` carries the model's own wording of the task;
   the helper also attaches the last few final user utterances, and prints one stdout line:
   `{"type":"request","id":...,"text":...,"userSaid":[...]}`.
2. The plugin queues it and submits `[voice] <text>` with `$.prompt.submit`, one request at a time
   (a session runs one turn at a time anyway).
3. A plugin's own hooks do not see its own `$.prompt.submit`, so the voice turn is recognised at
   `turn.start` by its text containing the submitted prompt; its `turnId` is recorded. Only the
   main-loop `turn.complete` of that turn (no `agentId`) counts as the answer.
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

## Thread label

`/voice on <name>` passes `--label` to the helper; the page can rename it (`POST /label`), and the
helper reports the new name on stdout so the plugin stores it in `$.store` under the session id.
The label goes into the page title and heading and into the voice prompt.

## Data and privacy

- Sent to OpenAI: microphone audio, transcripts, request texts, and Claude's trimmed answers.
- Local logs (in the data folder) contain transcripts and request texts; SDP ICE credentials,
  fingerprints, candidate IPs, bearer tokens, JWTs and API-key-like strings are redacted.
  `voice-answers.jsonl` records only id, status, length and turn id, never the answer text.
- The helper's HTTP server listens on `127.0.0.1` only and refuses POSTs whose `Origin` is not
  `http://127.0.0.1:*` or `http://localhost:*`.

## Known limits

- Delegation is the model's judgement: it can answer something itself that needed work, or delegate
  small talk. Tune with the persona's `instructions`.
- Consecutive requests are told apart only by the request text quoted in `[RESULT] ...`.
- Each delegation starts (and immediately interrupts) a codex turn, which may count slightly against
  your ChatGPT plan usage.
- Claude's permission prompts must be answered on screen.
- Requests run one at a time; parallel work would need one subagent per request and a new guard design.
